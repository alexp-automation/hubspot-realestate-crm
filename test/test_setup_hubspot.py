import copy
import unittest

from helpers import FakeTransport, quiet  # noqa: F401  (also puts the repo on sys.path)

import setup_hubspot as setup
from common.http_client import ApiClient, hubspot_client

DESIGN = setup.load_design()


def design_counts():
    n_props = sum(len(v) for v in DESIGN["properties"].values())
    n_groups = sum(len(v) for v in DESIGN["property_groups"].values()) + len(DESIGN["custom_objects"])
    n_labels = sum(len(a["labels"]) for a in DESIGN["associations"])
    n_assoc_defs = sum(1 for a in DESIGN["associations"]
                       if a["from"] not in setup.STANDARD_TYPE_IDS or a["to"] not in setup.STANDARD_TYPE_IDS)
    return n_props, n_groups, n_labels, n_assoc_defs


def portal_with_some_setup(transport):
    """A portal where Property exists, one contact property exists, Land Sales exists minus one stage."""
    prop_obj = DESIGN["custom_objects"][0]
    schema = {"name": "re_property", "objectTypeId": "2-111",
              "properties": [{"name": p["name"], "label": p["label"], "type": setup.FIELD_TYPES[p["type"]][0],
                              "options": [{"value": v} for v, _ in p.get("options", [])]}
                             for p in prop_obj["properties"]],
              "associations": [{"fromObjectTypeId": "2-111", "toObjectTypeId": "0-3"}]}
    transport.add("GET", r"/crm/v3/schemas$", {"results": [schema]})
    transport.add("GET", r"/crm/v3/properties/contacts$", {"results": [
        {"name": "preferred_language", "label": "Preferred language", "type": "enumeration",
         "options": [{"value": "ar"}, {"value": "en"}]},
        {"name": "client_type", "label": "Client type", "type": "enumeration",
         "options": [{"value": "individual"}]},
    ]})
    transport.add("GET", r"/crm/v3/properties/contacts/groups$", {"results": [{"name": "re_client_profile"}]})
    transport.add("GET", r"/crm/v3/properties/\w+(/groups)?$", {"results": []})
    transport.add("GET", r"/crm/v3/properties/2-111/groups$", {"results": []})
    land = DESIGN["pipelines"][0]
    transport.add("GET", r"/crm/v3/pipelines/deals$", {"results": [{
        "id": "77", "label": "Land Sales",
        "stages": [{"id": f"s{i}", "label": s["label"], "metadata": {"probability": str(s["probability"])}}
                   for i, s in enumerate(land["stages"]) if s["key"] != "due_diligence"]}]})
    transport.add("GET", r"/crm/v4/associations/contacts/deals/labels$",
                  {"results": [{"label": "Buyer", "typeId": 5}, {"label": None, "typeId": 3}]})
    transport.add("GET", r"/crm/v4/associations/.*/labels$", {"results": []})


class PlanTests(unittest.TestCase):
    def test_empty_portal_creates_everything(self):
        items = setup.plan(DESIGN, setup.empty_state())
        creates = [i for i in items if i["action"] == "create"]
        n_props, n_groups, n_labels, n_assoc = design_counts()
        expected = len(DESIGN["custom_objects"]) + n_groups + n_props + n_assoc + n_labels + len(DESIGN["pipelines"])
        self.assertEqual(len(creates), expected)
        self.assertFalse([i for i in items if i["action"] in ("exists", "differs")])

    def test_pipeline_bodies_have_probabilities(self):
        items = setup.plan(DESIGN, setup.empty_state())
        pipeline = next(i for i in items if i["desc"].startswith("Leasing"))
        stages = pipeline["request"][2]["stages"]
        self.assertEqual(len(stages), 8)
        self.assertEqual(stages[0]["metadata"]["probability"], "0.05")
        self.assertEqual(stages[-2]["metadata"]["probability"], "1.0")  # Signed
        self.assertEqual(stages[-1]["metadata"]["probability"], "0.0")  # Lost

    def test_every_stage_rule_refers_to_a_known_deal_property(self):
        known = {p["name"] for p in DESIGN["properties"]["deals"]} | {"amount", "closedate"}
        for pipeline in DESIGN["pipelines"]:
            for stage in pipeline["stages"]:
                for prop in stage["required_properties"]:
                    self.assertIn(prop, known, f"{pipeline['label']} / {stage['label']}")
                if stage["probability"] == 0.0:
                    self.assertIn("lost_reason", stage["required_properties"])


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        portal_with_some_setup(self.transport)
        self.client = hubspot_client(token="test", transport=self.transport, log=quiet, sleep=lambda s: None)
        self.state = setup.read_state(self.client, DESIGN)
        self.items = setup.plan(DESIGN, self.state)

    def find(self, text):
        return [i for i in self.items if text in i["desc"]]

    def test_existing_items_are_skipped(self):
        self.assertEqual(self.find("Property (re_property)")[0]["action"], "exists")
        self.assertEqual(self.find("contacts.preferred_language")[0]["action"], "exists")
        self.assertEqual(self.find("contacts -> deals: 'Buyer'")[0]["action"], "exists")
        self.assertEqual(self.find("deals <-> re_property")[0]["action"], "exists")
        self.assertEqual(self.find("contacts: Buyer / tenant profile")[0]["action"], "exists")
        land = [i for i in self.items if i["section"] == "Pipelines" and i["desc"] == "Land Sales"]
        self.assertEqual(land[0]["action"], "exists")

    def test_missing_items_are_created(self):
        self.assertEqual(self.find("Unit (re_unit)")[0]["action"], "create")
        self.assertEqual(self.find("contacts.budget_range_sar")[0]["action"], "create")
        self.assertEqual(self.find("stage Land Sales / Due diligence")[0]["action"], "create")
        self.assertEqual(self.find("Leasing with 8 stages")[0]["action"], "create")
        self.assertEqual(self.find("contacts -> deals: 'Tenant'")[0]["action"], "create")

    def test_differences_are_reported_not_overwritten(self):
        diff = self.find("contacts.client_type")[0]
        self.assertEqual(diff["action"], "differs")
        self.assertIn("missing options: company", diff["desc"])
        self.assertIn("label is 'Client type'", diff["desc"])

    def test_apply_only_sends_creates_and_fills_new_object_ids(self):
        self.transport.calls.clear()
        errors = setup.apply(self.items, self.client, self.state, log=quiet)
        self.assertEqual(errors, 0)
        writes = self.transport.writes()
        self.assertEqual(len(writes), sum(1 for i in self.items if i["action"] == "create"))
        urls = [w["url"] for w in writes]
        self.assertIn("https://api.hubapi.com/crm/v3/pipelines/deals/77/stages", urls)
        self.assertFalse(any("{{" in u for u in urls))
        # Unit schema was created first; later calls use its returned objectTypeId, existing Property keeps 2-111
        unit_id = next(w for w in writes if w["url"].endswith("/crm/v3/schemas"))
        unit_assoc = next(w for w in writes if w["body"] and w["body"].get("name") == "re_unit_to_re_property")
        self.assertEqual(unit_assoc["body"]["toObjectTypeId"], "2-111")
        self.assertTrue(unit_assoc["body"]["fromObjectTypeId"].startswith("2-9"))
        self.assertIsNotNone(unit_id)

    def test_second_run_after_full_setup_creates_nothing(self):
        state = copy.deepcopy(self.state)
        for obj in DESIGN["custom_objects"]:
            state["schemas"][obj["name"]] = {
                "name": obj["name"], "objectTypeId": "2-1" if obj["name"] == "re_property" else "2-2",
                "properties": [], "associations": [{"fromObjectTypeId": "0-3", "toObjectTypeId": "2-1"},
                                                   {"fromObjectTypeId": "2-2", "toObjectTypeId": "0-3"},
                                                   {"fromObjectTypeId": "2-2", "toObjectTypeId": "2-1"},
                                                   {"fromObjectTypeId": "2-2", "toObjectTypeId": "0-1"}]}
        for obj, props in DESIGN["properties"].items():
            state["properties"][obj] = {p["name"]: setup.property_body(p, p["group"]) for p in props}
        for obj in DESIGN["custom_objects"]:
            state["properties"][obj["name"]] = {p["name"]: setup.property_body(p) for p in obj["properties"]}
            state["groups"][obj["name"]] = {obj["group"]["name"]}
        for obj, groups in DESIGN["property_groups"].items():
            state["groups"][obj] = {g["name"] for g in groups}
        for a in DESIGN["associations"]:
            state["labels"][(a["from"], a["to"])] = {lb["label"] for lb in a["labels"]}
        for p in DESIGN["pipelines"]:
            state["pipelines"][p["label"]] = {"id": p["key"], "stages": [
                {"label": s["label"], "metadata": {"probability": str(s["probability"])}} for s in p["stages"]]}
        items = setup.plan(DESIGN, state)
        self.assertEqual([i["desc"] for i in items if i["action"] in ("create", "differs")], [])


class DryRunTests(unittest.TestCase):
    def test_dry_run_without_token_makes_no_http_calls(self):
        transport = FakeTransport()
        client = hubspot_client(dry_run=True, token="", transport=transport, log=quiet)
        items = setup.plan(DESIGN, setup.empty_state())
        setup.apply(items, client, setup.empty_state(), log=quiet)  # even apply only logs
        self.assertEqual(transport.calls, [])
        self.assertGreater(len(client.planned), 40)

    def test_dry_run_with_token_only_reads(self):
        transport = FakeTransport()
        portal_with_some_setup(transport)
        client = hubspot_client(dry_run=True, token="test", transport=transport, log=quiet)
        state = setup.read_state(client, DESIGN)
        setup.apply(setup.plan(DESIGN, state), client, state, log=quiet)
        self.assertTrue(transport.calls)
        self.assertEqual({c["method"] for c in transport.calls}, {"GET"})

    def test_main_dry_run(self):
        import io
        import contextlib
        import os
        old = os.environ.pop("HUBSPOT_TOKEN", None)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = setup.main(["--dry-run"])
        finally:
            if old is not None:
                os.environ["HUBSPOT_TOKEN"] = old
        self.assertEqual(code, 0)
        self.assertIn("nothing was changed", out.getvalue())


class RetryTests(unittest.TestCase):
    def test_429_is_retried_with_retry_after(self):
        transport = FakeTransport()
        transport.add("GET", r"/crm/v3/schemas", {"message": "rate limit"}, status=429,
                      headers={"Retry-After": "2"}, times=2)
        transport.add("GET", r"/crm/v3/schemas", {"results": []})
        sleeps = []
        client = ApiClient("https://api.hubapi.com", transport=transport, sleep=sleeps.append, log=quiet)
        self.assertEqual(client.get("/crm/v3/schemas"), {"results": []})
        self.assertEqual(sleeps, [2.0, 2.0])
        self.assertEqual(len(transport.calls), 3)

    def test_backoff_gives_up_after_max_retries(self):
        transport = FakeTransport()
        transport.add("GET", r".", {"message": "busy"}, status=503)
        sleeps = []
        client = ApiClient("https://x", transport=transport, sleep=sleeps.append, max_retries=3, log=quiet)
        with self.assertRaises(Exception):
            client.get("/a")
        self.assertEqual(sleeps, [1, 2, 4])


if __name__ == "__main__":
    unittest.main()
