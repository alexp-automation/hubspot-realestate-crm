import json
import os
import unittest

from helpers import ROOT, FakeTransport, quiet

import leads
import server
from common.http_client import hubspot_client

with open(os.path.join(ROOT, "config", "crm_design.json"), encoding="utf-8") as fh:
    DESIGN = json.load(fh)
with open(os.path.join(ROOT, "test", "sample-leads.json"), encoding="utf-8") as fh:
    SAMPLES = json.load(fh)


class PhoneTests(unittest.TestCase):
    def test_saudi_mobile_formats(self):
        for raw in ["0501234567", "501234567", "+966501234567", "+966 50 123 4567", "00966501234567",
                    "966-50-123-4567", "(050) 123 4567", "٠٥٠١٢٣٤٥٦٧", "+٩٦٦٥٠١٢٣٤٥٦٧", " 050 123 4567‏"]:
            self.assertEqual(leads.normalize_phone(raw), "+966501234567", raw)

    def test_landline_and_foreign(self):
        self.assertEqual(leads.normalize_phone("012 612 3456"), "+966126123456")
        self.assertEqual(leads.normalize_phone("+971 50 123 4567"), "+971501234567")

    def test_invalid(self):
        for raw in ["", None, "12345", "05012345", "0601234567", "abc"]:
            self.assertIsNone(leads.normalize_phone(raw), raw)


class LanguageTests(unittest.TestCase):
    def test_arabic(self):
        self.assertEqual(leads.detect_language("أبحث عن أرض في جدة"), "ar")
        self.assertEqual(leads.detect_language("نورة", "أريد مكتب 300 m2 في العليا"), "ar")

    def test_english(self):
        self.assertEqual(leads.detect_language("Looking for a plot in Jeddah"), "en")
        self.assertEqual(leads.detect_language(""), "en")

    def test_mostly_english_with_arabic_name(self):
        self.assertEqual(leads.detect_language("Please call me about the office on Olaya street, thanks - عمر"), "en")


class NormalizeTests(unittest.TestCase):
    def test_cities(self):
        self.assertEqual(leads.normalize_city("جدة"), "jeddah")
        self.assertEqual(leads.normalize_city(" Riyadh "), "riyadh")
        self.assertEqual(leads.normalize_city("Al Khobar"), "khobar")
        self.assertEqual(leads.normalize_city("Abha"), "other")
        self.assertIsNone(leads.normalize_city(""))

    def test_budget(self):
        self.assertEqual(leads.budget_range("2,500,000", "buy"), "buy_1m_3m")
        self.assertEqual(leads.budget_range("٤ مليون", "buy"), "buy_3m_10m")
        self.assertEqual(leads.budget_range("250 ألف", "rent"), "rent_100k_300k")
        self.assertEqual(leads.budget_range("90k per year", "rent"), "rent_under_100k")
        self.assertEqual(leads.budget_range("rent_1m_plus", "rent"), "rent_1m_plus")
        self.assertIsNone(leads.budget_range("flexible", "buy"))

    def test_website_parse_cleans_fields(self):
        lead = leads.parse_website(SAMPLES[0]["payload"])
        self.assertEqual((lead["first_name"], lead["last_name"]), ("Faisal", "Al-Otaibi"))
        self.assertEqual(lead["email"], "faisal.otaibi@example.com")
        self.assertEqual(lead["listing_id"], "JED-LND-0142")
        self.assertEqual(leads.validate(lead), [])


class RoutingTests(unittest.TestCase):
    def lead(self, **kw):
        base = {"name": "Test", "email": "t@example.com", "company_website": ""}
        return leads.parse_website({**base, **kw})

    def test_buy_goes_to_land_sales(self):
        self.assertEqual(leads.route(self.lead(interest="buy"))[0], "land_sales")
        self.assertEqual(leads.route(self.lead(message="أرغب في شراء أرض سكنية"))[0], "land_sales")

    def test_rent_goes_to_leasing(self):
        self.assertEqual(leads.route(self.lead(interest="rent", asset_type="office"))[0], "leasing")
        self.assertEqual(leads.route(self.lead(message="أبحث عن شقة للإيجار"))[0], "leasing")

    def test_asset_type_decides_when_no_intent(self):
        self.assertEqual(leads.route(self.lead(asset_type="land"))[0], "land_sales")
        self.assertEqual(leads.route(self.lead(message="Any shop in the strip mall?"))[0], "leasing")

    def test_unclear_defaults_to_land_sales(self):
        key, reason = leads.route(self.lead(message="Please call me"))
        self.assertEqual(key, "land_sales")
        self.assertIn("default", reason)


class MetaTests(unittest.TestCase):
    def test_graph_api_lead(self):
        [lead] = leads.parse_meta(SAMPLES[3]["payload"])
        self.assertEqual(lead["first_name"], "Sara")
        self.assertEqual(lead["phone"], "+966561112233")
        self.assertEqual(lead["intent"], "rent")
        self.assertEqual(lead["asset_type"], "apartment_building")
        self.assertEqual(lead["source"], "facebook_lead_ad")
        self.assertEqual(lead["meta_lead_id"], "1203456789012345")

    def test_webhook_envelope_arabic_instagram(self):
        [lead] = leads.parse_meta(SAMPLES[4]["payload"])
        self.assertEqual(lead["source"], "instagram_lead_ad")
        self.assertEqual(lead["language"], "ar")
        self.assertEqual(lead["city"], "jeddah")
        self.assertEqual(lead["intent"], "buy")
        self.assertEqual(lead["asset_type"], "land")
        self.assertEqual(lead["budget_range"], "buy_3m_10m")
        self.assertEqual(lead["phone"], "+966507654321")

    def test_multiple_leads_in_one_webhook(self):
        change = {"value": {"field_data": [{"name": "full_name", "values": ["A B"]}]}}
        payload = {"entry": [{"changes": [change, change]}, {"changes": [change]}]}
        self.assertEqual(len(leads.parse_meta(payload)), 3)

    def test_empty_payload(self):
        self.assertEqual(leads.parse_meta({"object": "page", "entry": []}), [])


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.t = FakeTransport()
        self.t.add("GET", r"/crm/v3/pipelines/deals$", {"results": [
            {"id": "101", "label": "Land Sales", "stages": [{"id": "1001", "label": "New inquiry"}]},
            {"id": "102", "label": "Leasing", "stages": [{"id": "2001", "label": "Inquiry"}]},
            {"id": "103", "label": "Renewals", "stages": [{"id": "3001", "label": "Renewal due"}]}]})
        self.t.add("GET", r"/crm/v3/schemas$", {"results": [{"name": "re_property", "objectTypeId": "2-555"}]})
        client = hubspot_client(token="test", transport=self.t, log=quiet)
        self.intake = server.LeadIntake(client, DESIGN, log=quiet)

    def calls(self, method, fragment):
        return [c for c in self.t.calls if c["method"] == method and fragment in c["url"]]

    def test_new_contact_new_deal_and_listing_link(self):
        self.t.add("POST", r"/objects/2-555/search", {"results": [{"id": "777"}]})
        self.t.add("POST", r"/objects/contacts/search", {"results": []})
        status, body = server.handle(self.intake, "website", SAMPLES[0]["payload"])
        self.assertEqual(status, 200)
        self.assertEqual(body["contact"], "created")
        created = self.calls("POST", "/crm/v3/objects/contacts")[-1]["body"]["properties"]
        self.assertEqual(created["re_lead_source"], "website_form")
        deal = self.calls("POST", "/crm/v3/objects/deals")[0]["body"]["properties"]
        self.assertEqual((deal["pipeline"], deal["dealstage"]), ("101", "1001"))
        self.assertEqual(deal["dealname"], "Faisal Al-Otaibi · Land plot · Jeddah")
        self.assertTrue(self.calls("PUT", "/associations/default/2-555/777"))
        self.assertTrue(self.calls("PUT", "/associations/default/contacts/"))

    def test_dedupe_by_email_updates_contact(self):
        self.t.add("POST", r"/objects/contacts/search", {"results": [{"id": "42"}]})
        status, body = server.handle(self.intake, "website", SAMPLES[1]["payload"])
        self.assertEqual(body["contact"], "updated")
        patch = self.calls("PATCH", "/crm/v3/objects/contacts/42")
        self.assertEqual(len(patch), 1)
        self.assertNotIn("re_lead_source", patch[0]["body"]["properties"])  # first touch kept
        self.assertEqual(len(self.calls("POST", "/contacts/search")), 1)  # phone search not needed
        deal = self.calls("POST", "/crm/v3/objects/deals")[0]["body"]["properties"]
        self.assertEqual((deal["pipeline"], deal["dealstage"]), ("102", "2001"))
        self.assertEqual(body["message"], server.THANKS["ar"])

    def test_dedupe_falls_back_to_phone(self):
        def search(url, body):
            field = body["filterGroups"][0]["filters"][0]["propertyName"]
            return {"results": [{"id": "43"}]} if field == "phone" else {"results": []}
        self.t.add("POST", r"/objects/contacts/search", search)
        status, body = server.handle(self.intake, "website", SAMPLES[0]["payload"] | {"listing_id": ""})
        self.assertEqual((body["contact"], body["contact_id"]), ("updated", "43"))
        phone_search = self.calls("POST", "/contacts/search")[1]["body"]
        self.assertEqual(len(phone_search["filterGroups"]), 2)  # phone OR mobilephone

    def test_broken_and_spam_leads_make_no_calls(self):
        status, body = server.handle(self.intake, "website", SAMPLES[6]["payload"])
        self.assertEqual(status, 400)
        self.assertEqual(body["errors"], ["name", "email", "phone", "contact"])
        status, body = server.handle(self.intake, "website", SAMPLES[5]["payload"])
        self.assertEqual((status, body.get("spam")), (200, True))
        self.assertEqual(self.t.calls, [])

    def test_meta_webhook_always_acknowledged(self):
        self.t.add("POST", r"/objects/contacts/search", {"results": []})
        status, body = server.handle(self.intake, "meta", SAMPLES[4]["payload"])
        self.assertEqual(status, 200)
        self.assertEqual(body["leads"][0]["pipeline"], "Land Sales")


class DryRunIntakeTests(unittest.TestCase):
    def test_all_samples_offline(self):
        t = FakeTransport()
        client = hubspot_client(dry_run=True, token="", transport=t, log=quiet)
        intake = server.LeadIntake(client, DESIGN, log=quiet)
        statuses = [server.handle(intake, s["channel"], s["payload"])[0] for s in SAMPLES]
        self.assertEqual(statuses, [200, 200, 200, 200, 200, 200, 400])
        self.assertEqual(t.calls, [])
        deal_posts = [c for c in client.planned if c["url"].endswith("/crm/v3/objects/deals")]
        self.assertEqual(len(deal_posts), 5)


if __name__ == "__main__":
    unittest.main()
