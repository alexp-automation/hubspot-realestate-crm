import json
import os
import unittest
from datetime import datetime, timezone

from helpers import ROOT, FakeTransport, quiet

import sync
from common.http_client import ApiClient, hubspot_client

MAPPING = sync.load_mapping()
KEY = MAPPING["alternate_key"]
ENTITIES = {e["hubspot_object"]: e for e in MAPPING["entities"]}
with open(os.path.join(ROOT, "test", "sample-hubspot-records.json"), encoding="utf-8") as fh:
    RECORDS = json.load(fh)
SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)


class MappingFileTests(unittest.TestCase):
    def test_every_field_has_a_valid_owner(self):
        for entity in MAPPING["entities"]:
            for field in entity["fields"]:
                self.assertIn(field["owner"], ("hubspot", "dynamics", "both"), field)

    def test_mapped_hubspot_properties_exist_in_crm_design(self):
        with open(os.path.join(ROOT, "config", "crm_design.json"), encoding="utf-8") as fh:
            design = json.load(fh)
        builtin = {"contacts": {"firstname", "lastname", "email", "jobtitle", "phone"},
                   "companies": {"name", "phone", "domain", "city"},
                   "deals": {"dealname", "amount", "closedate"}}
        for entity in MAPPING["entities"]:
            obj = entity["hubspot_object"]
            custom = {p["name"]: p for p in design["properties"][obj]}
            for field in entity["fields"]:
                name = field["hubspot"]
                self.assertTrue(name in custom or name in builtin[obj], f"{obj}.{name}")
                if field.get("transform") == "choice" and name in custom:
                    values = {v for v, _ in custom[name]["options"]}
                    self.assertEqual(set(field["map"]), values, f"{obj}.{name} choice map")


class PayloadTests(unittest.TestCase):
    def test_contact_payload(self):
        payload, skipped = sync.build_payload(RECORDS["contacts"][0], ENTITIES["contacts"], KEY)
        self.assertEqual(skipped, [])
        self.assertEqual(payload["emailaddress1"], "faisal.otaibi@example.com")
        self.assertEqual(payload["mobilephone"], "+966501234567")
        self.assertEqual(payload["address1_city"], "Jeddah")
        self.assertEqual(payload["new_clienttype"], 100000000)
        self.assertEqual(payload["new_preferredlanguage"], 100000001)
        self.assertNotIn("new_kycstatus", payload)  # owned by Dynamics, never pushed

    def test_closed_won_lease_maps_to_opportunity_with_lookups(self):
        payload, _ = sync.build_payload(RECORDS["deals"][1], ENTITIES["deals"], KEY)
        self.assertEqual(payload["estimatedvalue"], 780000)
        self.assertEqual(payload["new_annualrent"], 260000)
        self.assertEqual(payload["new_leasetermmonths"], 36)
        self.assertEqual(payload["estimatedclosedate"], "2026-10-07")
        self.assertEqual(payload["new_dealtype"], 100000001)
        self.assertEqual(payload["parentcontactid@odata.bind"], "/contacts(new_hubspotid='102')")
        self.assertEqual(payload["parentaccountid@odata.bind"], "/accounts(new_hubspotid='5001')")

    def test_conflict_rule(self):
        record = RECORDS["companies"][0]  # HubSpot modified 2026-10-07T14:05Z
        newer = {"modifiedon": "2026-10-08T08:00:00Z"}
        payload, skipped = sync.build_payload(record, ENTITIES["companies"], KEY, existing=newer)
        # fields owned by both keep the Dynamics value, HubSpot-owned fields still go through
        self.assertEqual(sorted(skipped), ["name", "new_crnumber", "telephone1"])
        self.assertEqual(payload["websiteurl"], "alofuq.example.sa")
        self.assertNotIn("new_vatnumber", payload)

        older = {"modifiedon": "2026-10-01T08:00:00Z"}
        payload, skipped = sync.build_payload(record, ENTITIES["companies"], KEY, existing=older)
        self.assertEqual(skipped, [])
        self.assertEqual(payload["new_crnumber"], "1010123456")

    def test_win_request(self):
        payload, _ = sync.build_payload(RECORDS["deals"][0], ENTITIES["deals"], KEY)
        body = sync.win_request(RECORDS["deals"][0], ENTITIES["deals"], KEY, payload)
        self.assertEqual(body["Status"], 3)
        self.assertEqual(body["OpportunityClose"]["opportunityid@odata.bind"], "/opportunities(new_hubspotid='9001')")
        self.assertEqual(body["OpportunityClose"]["actualrevenue"], 2350000)

    def test_parse_time_variants(self):
        a = sync.parse_time("2026-10-06T09:12:33.120Z")
        b = sync.parse_time("2026-10-06T09:12:33.1234567+00:00")
        c = sync.parse_time(str(int(a.timestamp() * 1000)))
        self.assertEqual(a, c)
        self.assertEqual(a.replace(microsecond=0), b.replace(microsecond=0))


class SyncRunTests(unittest.TestCase):
    def test_sample_filter_skips_open_deals(self):
        ids = [r["id"] for r in sync.load_sample(os.path.join(ROOT, "test", "sample-hubspot-records.json"),
                                                 ENTITIES["deals"], SINCE)]
        self.assertEqual(ids, ["9001", "9002"])

    def test_dry_run_makes_no_calls(self):
        t = FakeTransport()
        dyn = ApiClient("https://org.crm4.dynamics.com/api/data/v9.2", dry_run=True, transport=t, log=quiet)
        stats = sync.sync_entity(dyn, RECORDS["deals"][:2], ENTITIES["deals"], KEY, log=quiet)
        self.assertEqual(t.calls, [])
        self.assertEqual(stats["won"], 2)
        self.assertEqual([c["method"] for c in dyn.planned], ["PATCH", "POST", "PATCH", "POST"])
        self.assertTrue(dyn.planned[0]["url"].endswith("/opportunities(new_hubspotid='9001')"))

    def test_live_upsert_skips_win_when_already_won(self):
        t = FakeTransport()
        t.add("GET", r"opportunities\(new_hubspotid='9001'\)", {"modifiedon": "2026-10-01T00:00:00Z", "statecode": 1})
        t.add("GET", r"opportunities\(new_hubspotid='9002'\)", {"error": {"message": "not found"}}, status=404)
        t.add("PATCH", r".", None, status=204)
        dyn = ApiClient("https://org.crm4.dynamics.com/api/data/v9.2", transport=t, log=quiet)
        stats = sync.sync_entity(dyn, RECORDS["deals"][:2], ENTITIES["deals"], KEY, log=quiet)
        self.assertEqual((stats["updated"], stats["created"], stats["won"]), (1, 1, 1))
        wins = [c for c in t.calls if c["url"].endswith("/WinOpportunity")]
        self.assertEqual(len(wins), 1)
        self.assertIn("9002", wins[0]["body"]["OpportunityClose"]["opportunityid@odata.bind"])

    def test_fetch_changed_pages_and_reads_associations(self):
        t = FakeTransport()
        pages = iter([
            {"results": [{"id": "9001", "properties": {}}], "paging": {"next": {"after": "1"}}},
            {"results": [{"id": "9002", "properties": {}}]},
        ])
        t.add("POST", r"/crm/v3/objects/deals/search", lambda url, body: next(pages))
        t.add("GET", r"/associations/contacts", {"results": [{"toObjectId": 101}]})
        t.add("GET", r"/associations/companies", {"results": []})
        hs = hubspot_client(token="test", transport=t, log=quiet)
        records = list(sync.fetch_changed(hs, ENTITIES["deals"], SINCE))
        self.assertEqual([r["id"] for r in records], ["9001", "9002"])
        self.assertEqual(records[0]["associations"], {"contacts": ["101"], "companies": []})
        searches = [c["body"] for c in t.calls if c["url"].endswith("/search")]
        self.assertEqual(searches[1]["after"], "1")
        filters = searches[0]["filterGroups"][0]["filters"]
        self.assertEqual(filters[0]["propertyName"], "hs_lastmodifieddate")
        self.assertEqual(filters[1], {"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "true"})


if __name__ == "__main__":
    unittest.main()
