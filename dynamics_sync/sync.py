#!/usr/bin/env python3
"""One-way sync HubSpot -> Microsoft Dynamics 365 (Dataverse Web API).

Pulls contacts, companies and closed-won deals changed since a timestamp and upserts them
into contact / account / opportunity using the alternate key new_hubspotid.

    python3 dynamics_sync/sync.py --dry-run --sample test/sample-hubspot-records.json   # fully offline
    python3 dynamics_sync/sync.py --dry-run --since 2026-10-01T00:00:00Z               # read HubSpot, print upserts
    python3 dynamics_sync/sync.py                                                      # live, since last run

Env: HUBSPOT_TOKEN, AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
     DYNAMICS_URL (e.g. https://yourorg.crm4.dynamics.com)
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from common.http_client import ApiClient, ApiError, hubspot_client, urllib_transport  # noqa: E402

MAPPING = os.path.join(HERE, "field_mapping.json")
STATE_FILE = os.path.join(HERE, ".last_sync.json")
AZURE_ENV = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "DYNAMICS_URL")


def load_mapping(path=MAPPING):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def parse_time(value):
    """ISO string (with Z or offset, any fraction) or epoch ms -> aware datetime."""
    if value in (None, ""):
        return None
    text = str(value)
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
    text = text.replace("Z", "+00:00")
    # Python 3.9 only accepts 3 or 6 fraction digits; pad or trim to 6
    text = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), text)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- mapping

def transform(value, field):
    if value in (None, ""):
        return None
    kind = field.get("transform", "text")
    if kind == "text":
        return str(value)
    if kind == "number":
        number = float(value)
        return int(number) if number.is_integer() else number
    if kind == "date":
        text = str(value)
        return parse_time(text).date().isoformat() if text.isdigit() else text[:10]
    if kind in ("map", "choice"):
        return field["map"].get(value)
    raise ValueError(f"unknown transform {kind}")


def key_path(entity_set, hubspot_id, alternate_key):
    return f"/{entity_set}({alternate_key}='{hubspot_id}')"


def build_payload(record, entity, alternate_key, existing=None):
    """Dataverse body for one HubSpot record, applying field ownership and the conflict rule.

    Returns (payload, skipped) where skipped lists fields left out because Dynamics had
    the newer value.
    """
    props = record.get("properties", {})
    hubspot_modified = parse_time(props.get(entity["modified_property"]))
    dynamics_modified = parse_time((existing or {}).get("modifiedon"))
    dynamics_newer = bool(hubspot_modified and dynamics_modified and dynamics_modified > hubspot_modified)

    payload, skipped = {}, []
    for field in entity["fields"]:
        if field["owner"] == "dynamics":
            continue  # Dynamics is the source of truth, never overwrite it from HubSpot
        value = transform(props.get(field["hubspot"]), field)
        if value is None:
            continue
        if field["owner"] == "both" and dynamics_newer:
            skipped.append(field["dynamics"])
            continue
        payload[field["dynamics"]] = value

    for lookup in entity.get("lookups", []):
        ids = record.get("associations", {}).get(lookup["association"]) or []
        if ids:
            payload[lookup["dynamics"]] = key_path(lookup["entity_set"], ids[0], alternate_key)
    return payload, skipped


def win_request(record, entity, alternate_key, payload):
    """Body for the WinOpportunity action (Dataverse does not allow setting statecode=Won by update)."""
    body = {
        "Status": entity["mark_won"]["statuscode"],
        "OpportunityClose": {
            "subject": f"Won: {payload.get('name', record['id'])}",
            "opportunityid@odata.bind": key_path(entity["entity_set"], record["id"], alternate_key),
        },
    }
    if "estimatedvalue" in payload:
        body["OpportunityClose"]["actualrevenue"] = payload["estimatedvalue"]
    if "estimatedclosedate" in payload:
        body["OpportunityClose"]["actualend"] = payload["estimatedclosedate"]
    return body


# ---------------------------------------------------------------- HubSpot side

def fetch_changed(hs, entity, since):
    """All records of this object changed since `since` (HubSpot search API, paged)."""
    filters = [{"propertyName": entity["modified_property"], "operator": "GTE",
                "value": str(int(since.timestamp() * 1000))}]
    if entity.get("only_when"):
        filters.append({"propertyName": entity["only_when"]["property"], "operator": "EQ",
                        "value": entity["only_when"]["value"]})
    properties = sorted({f["hubspot"] for f in entity["fields"]} | {entity["modified_property"]})
    after = None
    while True:
        body = {"filterGroups": [{"filters": filters}], "properties": properties,
                "sorts": [{"propertyName": entity["modified_property"], "direction": "ASCENDING"}], "limit": 100}
        if after:
            body["after"] = after
        resp = hs.post(f"/crm/v3/objects/{entity['hubspot_object']}/search", body)
        for record in resp.get("results", []):
            if entity.get("lookups"):
                record["associations"] = {
                    lk["association"]: [str(r["toObjectId"]) for r in hs.get(
                        f"/crm/v4/objects/{entity['hubspot_object']}/{record['id']}/associations/{lk['association']}"
                    ).get("results", [])]
                    for lk in entity["lookups"]}
            yield record
        after = resp.get("paging", {}).get("next", {}).get("after")
        if not after:
            return


def load_sample(path, entity, since):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for record in data.get(entity["hubspot_object"], []):
        props = record["properties"]
        modified = parse_time(props.get(entity["modified_property"]))
        if modified and modified < since:
            continue
        cond = entity.get("only_when")
        if cond and str(props.get(cond["property"])).lower() != cond["value"]:
            continue
        yield record


# ---------------------------------------------------------------- Dynamics side

def azure_token(transport=urllib_transport):
    env = {k: os.environ[k] for k in AZURE_ENV}
    url = f"https://login.microsoftonline.com/{env['AZURE_TENANT_ID']}/oauth2/v2.0/token"
    form = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": env["AZURE_CLIENT_ID"],
        "client_secret": env["AZURE_CLIENT_SECRET"], "scope": env["DYNAMICS_URL"].rstrip("/") + "/.default",
    }).encode()
    status, _, raw = transport("POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, form)
    if status != 200:
        raise SystemExit(f"Azure token request failed ({status}): {raw[:300]!r}")
    return json.loads(raw)["access_token"]


def dynamics_client(mapping, dry_run, transport=None, log=print):
    has_creds = all(os.environ.get(k) for k in AZURE_ENV)
    if not has_creds and not dry_run:
        raise SystemExit("Set " + ", ".join(AZURE_ENV) + " or run with --dry-run.")
    base = os.environ.get("DYNAMICS_URL", "https://yourorg.crm4.dynamics.com").rstrip("/")
    headers = {"OData-MaxVersion": "4.0", "OData-Version": "4.0"}
    if has_creds:
        headers["Authorization"] = "Bearer " + azure_token(transport or urllib_transport)
    return ApiClient(f"{base}/api/data/{mapping['api_version']}", headers=headers, dry_run=dry_run,
                     live_reads=has_creds, transport=transport, log=log, name="dynamics")


def get_existing(dyn, entity, hubspot_id, alternate_key):
    if dyn.dry_run and not dyn.live_reads:
        return None
    select = "modifiedon,statecode" if entity.get("mark_won") else "modifiedon"
    try:
        return dyn.get(key_path(entity["entity_set"], hubspot_id, alternate_key), {"$select": select})
    except ApiError as err:
        if err.status == 404:
            return None
        raise


def sync_entity(dyn, records, entity, alternate_key, log=print):
    stats = {"created": 0, "updated": 0, "won": 0, "conflicts": 0, "errors": 0}
    for record in records:
        try:
            existing = get_existing(dyn, entity, record["id"], alternate_key)
            payload, skipped = build_payload(record, entity, alternate_key, existing)
            if skipped:
                stats["conflicts"] += 1
                log(f"  {entity['dynamics_entity']} {record['id']}: Dynamics is newer, kept its "
                    + ", ".join(skipped))
            dyn.patch(key_path(entity["entity_set"], record["id"], alternate_key), payload)
            stats["updated" if existing else "created"] += 1
            if entity.get("mark_won") and (existing or {}).get("statecode") != entity["mark_won"]["statecode"]:
                dyn.post("/WinOpportunity", win_request(record, entity, alternate_key, payload))
                stats["won"] += 1
        except ApiError as err:
            stats["errors"] += 1
            log(f"  ! {entity['dynamics_entity']} {record['id']}: {err}")
    return stats


def read_since(arg):
    if arg:
        return parse_time(arg)
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return parse_time(json.load(fh)["last_run"])
    except (OSError, KeyError, ValueError):
        return datetime.now(timezone.utc) - timedelta(days=1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync HubSpot changes into Dynamics 365")
    parser.add_argument("--since", help="ISO time, default: last successful run or 24h ago")
    parser.add_argument("--dry-run", action="store_true", help="print planned upserts, write nothing")
    parser.add_argument("--sample", help="read HubSpot records from a JSON file instead of the API")
    args = parser.parse_args(argv)

    mapping = load_mapping()
    alternate_key = mapping["alternate_key"]
    since = read_since(args.since)
    started = datetime.now(timezone.utc)
    hs = None if args.sample else hubspot_client(dry_run=False)
    dyn = dynamics_client(mapping, args.dry_run)

    print(f"Syncing HubSpot changes since {since.isoformat()}" + (" (dry run)" if args.dry_run else ""))
    if dyn.dry_run and not dyn.live_reads:
        print("No Dynamics credentials: existing records are not checked, every record counts as new.")
    errors = 0
    for entity in mapping["entities"]:
        print(f"\n{entity['hubspot_object']} -> {entity['dynamics_entity']}")
        records = (load_sample(args.sample, entity, since) if args.sample
                   else fetch_changed(hs, entity, since))
        stats = sync_entity(dyn, records, entity, alternate_key)
        errors += stats["errors"]
        print("  " + ", ".join(f"{k}: {v}" for k, v in stats.items()))

    if not args.dry_run and not errors:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"last_run": started.isoformat()}, fh)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
