#!/usr/bin/env python3
"""Webhook server that turns website and Meta Lead Ads inquiries into HubSpot contacts and deals.

    python3 lead_intake/server.py --dry-run --file test/sample-leads.json   # process samples, no API calls
    python3 lead_intake/server.py --dry-run --port 8080                     # run the server, log calls only
    HUBSPOT_TOKEN=pat-... python3 lead_intake/server.py --port 8080         # live

Endpoints:
    POST /leads/website     JSON (or form-encoded) from inquiry-form.html
    GET  /webhooks/meta     Meta subscription check (hub.challenge), needs META_VERIFY_TOKEN
    POST /webhooks/meta     Meta Lead Ads payload with field_data
    GET  /health
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from common.http_client import ApiError, hubspot_client  # noqa: E402
import leads  # noqa: E402

CONFIG = os.path.join(os.path.dirname(HERE), "config", "crm_design.json")
MAX_BODY = 64 * 1024
THANKS = {
    "en": "Thank you, our team will contact you shortly.",
    "ar": "شكراً لتواصلكم، سيتواصل معكم فريقنا قريباً.",
}
ASSET_LABELS = {"land": "Land plot", "office": "Office", "retail": "Retail", "apartment_building": "Apartment"}


class LeadIntake:
    """Creates or updates the contact, opens a deal in the right pipeline, links the listing."""

    def __init__(self, client, design, log=print):
        self.client = client
        self.design = design
        self.log = log
        self._ids = None

    # HubSpot assigns ids to pipelines, stages and custom objects, so look them up by label once.
    def ids(self):
        if self._ids is not None:
            return self._ids
        ids = {"pipelines": {}, "property_type": None}
        live = not self.client.dry_run or self.client.live_reads
        portal = self.client.get("/crm/v3/pipelines/deals").get("results", []) if live else []
        for pipeline in self.design["pipelines"]:
            stage = next(s for s in pipeline["stages"] if s["key"] == pipeline["intake_stage"])
            found = next((p for p in portal if p["label"] == pipeline["label"]), None)
            if found:
                stage_id = next((s["id"] for s in found["stages"] if s["label"] == stage["label"]), None)
                ids["pipelines"][pipeline["key"]] = (found["id"], stage_id)
            else:
                ids["pipelines"][pipeline["key"]] = (f"<{pipeline['label']}>", f"<{stage['label']}>")
        schemas = self.client.get("/crm/v3/schemas").get("results", []) if live else []
        ids["property_type"] = next((s["objectTypeId"] for s in schemas if s["name"] == "re_property"),
                                    "<re_property>")
        self._ids = ids
        return ids

    def find_contact(self, lead):
        searches = []
        if lead["email"]:
            searches.append([{"filters": [{"propertyName": "email", "operator": "EQ", "value": lead["email"]}]}])
        if lead["phone"]:
            searches.append([
                {"filters": [{"propertyName": "phone", "operator": "EQ", "value": lead["phone"]}]},
                {"filters": [{"propertyName": "mobilephone", "operator": "EQ", "value": lead["phone"]}]},
            ])
        for groups in searches:  # email first, then phone
            resp = self.client.post("/crm/v3/objects/contacts/search", {
                "filterGroups": groups, "properties": ["email", "phone", "firstname"], "limit": 1})
            if resp.get("results"):
                return resp["results"][0]["id"]
        return None

    def contact_properties(self, lead, is_new):
        props = {
            "firstname": lead["first_name"],
            "lastname": lead["last_name"],
            "email": lead["email"],
            "phone": lead["phone"],
            "mobilephone": lead["phone"] if lead["phone"] and lead["phone"].startswith("+9665") else None,
            "company": lead["company"],
            "client_type": lead["client_type"],
            "budget_range_sar": lead["budget_range"],
            "preferred_asset_type": lead["asset_type"],
            "preferred_city": lead["city"],
            "preferred_language": lead["language"],
        }
        if is_new:
            props["re_lead_source"] = lead["source"]  # first touch is kept on later inquiries
        return {k: v for k, v in props.items() if v}

    def process(self, lead):
        pipeline_key, reason = leads.route(lead)
        pipeline = next(p for p in self.design["pipelines"] if p["key"] == pipeline_key)
        pipeline_id, stage_id = self.ids()["pipelines"][pipeline_key]

        contact_id = self.find_contact(lead)
        if contact_id:
            self.client.patch(f"/crm/v3/objects/contacts/{contact_id}",
                              {"properties": self.contact_properties(lead, is_new=False)})
            contact_action = "updated"
        else:
            contact_id = self.client.post("/crm/v3/objects/contacts",
                                          {"properties": self.contact_properties(lead, is_new=True)})["id"]
            contact_action = "created"

        name = f"{lead['first_name']} {lead['last_name']}".strip()
        parts = [name, ASSET_LABELS.get(lead["asset_type"], pipeline["label"]), (lead["city"] or "").title()]
        deal_props = {
            "dealname": " · ".join(p for p in parts if p),
            "pipeline": pipeline_id,
            "dealstage": stage_id,
            "deal_type": pipeline["deal_type"],
            "asset_type": lead["asset_type"],
            "re_lead_source": lead["source"],
            "description": lead["message"],
        }
        deal_id = self.client.post("/crm/v3/objects/deals",
                                   {"properties": {k: v for k, v in deal_props.items() if v}})["id"]
        self.client.put(f"/crm/v4/objects/deals/{deal_id}/associations/default/contacts/{contact_id}")

        listing = None
        if lead["listing_id"]:
            listing = self.link_listing(deal_id, lead["listing_id"])

        return {"contact": contact_action, "contact_id": contact_id, "deal_id": deal_id,
                "pipeline": pipeline["label"], "stage": pipeline["intake_stage"], "reason": reason,
                "language": lead["language"], "listing": listing}

    def link_listing(self, deal_id, listing_id):
        prop_type = self.ids()["property_type"]
        resp = self.client.post(f"/crm/v3/objects/{prop_type}/search", {
            "filterGroups": [{"filters": [{"propertyName": "listing_id", "operator": "EQ", "value": listing_id}]}],
            "properties": ["property_name", "listing_status"], "limit": 1})
        if not resp.get("results"):
            if self.client.dry_run and not self.client.live_reads:
                property_id = f"<property {listing_id}>"
            else:
                self.log(f"  listing {listing_id} not found, deal left without a property")
                return "not found"
        else:
            property_id = resp["results"][0]["id"]
        self.client.put(f"/crm/v4/objects/deals/{deal_id}/associations/default/{prop_type}/{property_id}")
        return "linked"


def handle(intake, channel, payload):
    """Validate and process one request body. Returns (http_status, response_dict)."""
    parsed = [leads.parse_website(payload)] if channel == "website" else leads.parse_meta(payload)
    if not parsed:
        return 400, {"ok": False, "errors": ["no_lead_data"]}
    results = []
    for lead in parsed:
        if lead["honeypot"]:
            results.append({"ok": True, "spam": True})  # pretend success, store nothing
            continue
        errors = leads.validate(lead)
        if errors:
            results.append({"ok": False, "errors": errors})
            continue
        try:
            outcome = intake.process(lead)
        except ApiError as err:
            intake.log(f"  HubSpot error: {err}")
            results.append({"ok": False, "errors": ["crm_unavailable"]})
            continue
        results.append({"ok": True, "message": THANKS[lead["language"]], **outcome})

    if channel == "website":
        first = results[0]
        status = 200 if first["ok"] else (502 if first["errors"] == ["crm_unavailable"] else 400)
        return status, first
    # Meta retries anything that is not 200, so always acknowledge and report per lead.
    return 200, {"ok": all(r["ok"] for r in results), "leads": results}


class Handler(BaseHTTPRequestHandler):
    intake = None

    def _send(self, status, body, content_type="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", os.environ.get("ALLOWED_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", os.environ.get("ALLOWED_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/health":
            return self._send(200, {"ok": True})
        if url.path == "/webhooks/meta":
            q = dict(urllib.parse.parse_qsl(url.query))
            expected = os.environ.get("META_VERIFY_TOKEN")
            if expected and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == expected:
                return self._send(200, q.get("hub.challenge", ""), "text/plain")
            return self._send(403, {"ok": False})
        self._send(404, {"ok": False})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        channel = {"/leads/website": "website", "/webhooks/meta": "meta"}.get(path)
        if not channel:
            return self._send(404, {"ok": False})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, {"ok": False, "errors": ["too_large"]})
        raw = self.rfile.read(length)

        if channel == "meta" and not meta_signature_ok(raw, self.headers.get("X-Hub-Signature-256")):
            return self._send(403, {"ok": False, "errors": ["bad_signature"]})
        try:
            if "application/x-www-form-urlencoded" in (self.headers.get("Content-Type") or ""):
                payload = dict(urllib.parse.parse_qsl(raw.decode("utf-8")))
            else:
                payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"ok": False, "errors": ["invalid_json"]})
        if not isinstance(payload, dict):
            return self._send(400, {"ok": False, "errors": ["invalid_json"]})

        status, body = handle(self.intake, channel, payload)
        self._send(status, body)


def meta_signature_ok(raw, header):
    """Check X-Hub-Signature-256 when META_APP_SECRET is set."""
    secret = os.environ.get("META_APP_SECRET")
    if not secret:
        return True
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def run_samples(intake, path):
    with open(path, encoding="utf-8") as fh:
        samples = json.load(fh)
    for sample in samples:
        print(f"\n=== {sample['description']} ({sample['channel']})")
        status, body = handle(intake, sample["channel"], sample["payload"])
        print(f"  -> HTTP {status} {json.dumps(body, ensure_ascii=False)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lead intake webhook for HubSpot")
    parser.add_argument("--dry-run", action="store_true", help="log HubSpot calls instead of sending them")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    parser.add_argument("--file", help="process leads from a samples JSON file and exit")
    args = parser.parse_args(argv)

    with open(CONFIG, encoding="utf-8") as fh:
        design = json.load(fh)
    intake = LeadIntake(hubspot_client(dry_run=args.dry_run), design)

    if args.file:
        run_samples(intake, args.file)
        return 0

    Handler.intake = intake
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    mode = "dry run" if args.dry_run else "live"
    print(f"Lead intake listening on :{args.port} ({mode})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
