#!/usr/bin/env python3
"""Create the CRM design from config/crm_design.json in a HubSpot portal.

Reads what already exists, creates only what is missing and reports anything that
differs from the config. Safe to run again after you edit the config.

    python3 setup_hubspot.py --dry-run            # plan only, no API calls without a token
    HUBSPOT_TOKEN=pat-... python3 setup_hubspot.py --dry-run   # plan against the real portal (reads only)
    HUBSPOT_TOKEN=pat-... python3 setup_hubspot.py             # apply
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common.http_client import ApiError, hubspot_client  # noqa: E402

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "crm_design.json")
STANDARD_TYPE_IDS = {"contacts": "0-1", "companies": "0-2", "deals": "0-3"}
FIELD_TYPES = {
    "text": ("string", "text"),
    "textarea": ("string", "textarea"),
    "number": ("number", "number"),
    "currency": ("number", "number"),
    "date": ("date", "date"),
    "select": ("enumeration", "select"),
}
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def load_design(path=DEFAULT_CONFIG):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def property_body(prop, group=None):
    type_, field_type = FIELD_TYPES[prop["type"]]
    body = {"name": prop["name"], "label": prop["label"], "type": type_, "fieldType": field_type}
    if group:
        body["groupName"] = group
    if prop.get("description"):
        body["description"] = prop["description"]
    if prop.get("unique"):
        body["hasUniqueValue"] = True
    if prop["type"] == "currency":
        body["showCurrencySymbol"] = True
    if "options" in prop:
        body["options"] = [{"label": label, "value": value, "displayOrder": i}
                           for i, (value, label) in enumerate(prop["options"])]
    return body


def property_diff(prop, existing):
    """Human-readable differences between a config property and the portal's version."""
    diffs = []
    if existing.get("label") != prop["label"]:
        diffs.append(f"label is '{existing.get('label')}'")
    want_type = FIELD_TYPES[prop["type"]][0]
    if existing.get("type") and existing["type"] != want_type:
        diffs.append(f"type is {existing['type']}, config says {want_type}")
    if "options" in prop:
        have = {o.get("value") for o in existing.get("options", [])}
        missing = [value for value, _ in prop["options"] if value not in have]
        if missing:
            diffs.append("missing options: " + ", ".join(missing))
    return diffs


# ---------------------------------------------------------------- read portal state

def custom_names(design):
    return [obj["name"] for obj in design["custom_objects"]]


def type_ref(name, state):
    """Object type usable in API paths: 'deals' for standard objects, '2-1234' for custom ones."""
    if name in STANDARD_TYPE_IDS:
        return name
    schema = state["schemas"].get(name)
    return schema["objectTypeId"] if schema else None


def type_id(name, state):
    if name in STANDARD_TYPE_IDS:
        return STANDARD_TYPE_IDS[name]
    return type_ref(name, state)


def empty_state():
    return {"schemas": {}, "properties": {}, "groups": {}, "pipelines": {}, "labels": {}}


def read_state(client, design):
    state = empty_state()
    for schema in client.get("/crm/v3/schemas").get("results", []):
        state["schemas"][schema["name"]] = schema

    for obj in set(design["properties"]) | set(design["property_groups"]):
        props = client.get(f"/crm/v3/properties/{obj}").get("results", [])
        state["properties"][obj] = {p["name"]: p for p in props}
        groups = client.get(f"/crm/v3/properties/{obj}/groups").get("results", [])
        state["groups"][obj] = {g["name"] for g in groups}

    for name in custom_names(design):
        schema = state["schemas"].get(name)
        if schema:
            state["properties"][name] = {p["name"]: p for p in schema.get("properties", [])}
            groups = client.get(f"/crm/v3/properties/{schema['objectTypeId']}/groups").get("results", [])
            state["groups"][name] = {g["name"] for g in groups}

    for pipeline in client.get("/crm/v3/pipelines/deals").get("results", []):
        state["pipelines"][pipeline["label"]] = pipeline

    for assoc in design["associations"]:
        a, b = type_ref(assoc["from"], state), type_ref(assoc["to"], state)
        if a and b:
            labels = client.get(f"/crm/v4/associations/{a}/{b}/labels").get("results", [])
            state["labels"][(assoc["from"], assoc["to"])] = {lb.get("label") for lb in labels if lb.get("label")}
    return state


# ---------------------------------------------------------------- plan

def plan(design, state):
    """Compare the design with the portal state. Returns a list of plan items.

    Each item: {section, action: create|exists|differs|manual, desc, request?, registers?}
    Paths and bodies may contain {{re_property}} placeholders for custom objects that
    do not exist yet; they are filled in during apply.
    """
    items = []

    def add(section, action, desc, request=None, registers=None):
        items.append({"section": section, "action": action, "desc": desc,
                      "request": request, "registers": registers})

    def ref(name):
        return type_ref(name, state) or "{{%s}}" % name

    def tid(name):
        return type_id(name, state) or "{{%s}}" % name

    # 1. custom object schemas
    for obj in design["custom_objects"]:
        name = obj["name"]
        label = obj["labels"]["singular"]
        if name in state["schemas"]:
            add("Custom objects", "exists", f"{label} ({name})")
            continue
        body = {
            "name": name,
            "labels": obj["labels"],
            "primaryDisplayProperty": obj["primary_display_property"],
            "secondaryDisplayProperties": obj["secondary_display_properties"],
            "searchableProperties": obj["searchable_properties"],
            "requiredProperties": obj["required_properties"],
            "properties": [property_body(p) for p in obj["properties"]],
            "associatedObjects": [],
        }
        add("Custom objects", "create", f"{label} ({name}) with {len(obj['properties'])} properties",
            ("POST", "/crm/v3/schemas", body), registers=name)

    # 2. property groups
    groups = {obj: list(gs) for obj, gs in design["property_groups"].items()}
    for obj in design["custom_objects"]:
        groups[obj["name"]] = [obj["group"]]
    for obj, obj_groups in groups.items():
        for group in obj_groups:
            if group["name"] in state["groups"].get(obj, set()):
                add("Property groups", "exists", f"{obj}: {group['label']}")
            else:
                add("Property groups", "create", f"{obj}: {group['label']}",
                    ("POST", f"/crm/v3/properties/{ref(obj)}/groups",
                     {"name": group["name"], "label": group["label"]}))

    # 3. properties (custom object properties are created with the schema; only add missing ones later)
    targets = [(obj, p, p["group"]) for obj, props in design["properties"].items() for p in props]
    for obj in design["custom_objects"]:
        if obj["name"] in state["schemas"]:
            targets += [(obj["name"], p, obj["group"]["name"]) for p in obj["properties"]]
    for obj, prop, group in targets:
        existing = state["properties"].get(obj, {}).get(prop["name"])
        desc = f"{obj}.{prop['name']} ({prop['label']})"
        if existing is None:
            add("Properties", "create", desc,
                ("POST", f"/crm/v3/properties/{ref(obj)}", property_body(prop, group)))
        else:
            diffs = property_diff(prop, existing)
            add("Properties", "differs" if diffs else "exists",
                desc + (": " + "; ".join(diffs) if diffs else ""))

    # 4. association definitions (custom objects only; standard pairs are built in)
    for assoc in design["associations"]:
        a, b = assoc["from"], assoc["to"]
        custom = [n for n in (a, b) if n not in STANDARD_TYPE_IDS]
        if not custom:
            continue
        exists = False
        schema = state["schemas"].get(custom[0])
        if schema and type_id(a, state) and type_id(b, state):
            pair = {type_id(a, state), type_id(b, state)}
            exists = any({x.get("fromObjectTypeId"), x.get("toObjectTypeId")} == pair
                         for x in schema.get("associations", []))
        desc = f"{a} <-> {b}"
        if exists:
            add("Associations", "exists", desc)
        else:
            owner = custom[0]
            other = b if owner == a else a
            add("Associations", "create", desc,
                ("POST", f"/crm/v3/schemas/{ref(owner)}/associations",
                 {"fromObjectTypeId": tid(owner), "toObjectTypeId": tid(other), "name": f"{owner}_to_{other}"}))

    # 5. association labels
    for assoc in design["associations"]:
        have = state["labels"].get((assoc["from"], assoc["to"]), set())
        for label in assoc["labels"]:
            desc = f"{assoc['from']} -> {assoc['to']}: '{label['label']}'"
            if label["label"] in have:
                add("Association labels", "exists", desc)
            else:
                add("Association labels", "create", desc,
                    ("POST", f"/crm/v4/associations/{ref(assoc['from'])}/{ref(assoc['to'])}/labels",
                     {"label": label["label"], "name": label["name"]}))

    # 6. deal pipelines and stages
    for order, pipeline in enumerate(design["pipelines"]):
        existing = state["pipelines"].get(pipeline["label"])
        stages = [stage_body(s, i) for i, s in enumerate(pipeline["stages"])]
        if existing is None:
            add("Pipelines", "create", f"{pipeline['label']} with {len(stages)} stages",
                ("POST", "/crm/v3/pipelines/deals",
                 {"label": pipeline["label"], "displayOrder": order + 1, "stages": stages}))
            continue
        add("Pipelines", "exists", pipeline["label"])
        have = {s["label"]: s for s in existing.get("stages", [])}
        for body in stages:
            current = have.get(body["label"])
            desc = f"{pipeline['label']} / {body['label']}"
            if current is None:
                add("Pipelines", "create", f"stage {desc}",
                    ("POST", f"/crm/v3/pipelines/deals/{existing['id']}/stages", body))
                continue
            have_p = current.get("metadata", {}).get("probability")
            if have_p is not None and float(have_p) != float(body["metadata"]["probability"]):
                add("Pipelines", "differs",
                    f"stage {desc}: probability is {have_p}, config says {body['metadata']['probability']}")

    # 7. required properties per stage: set in the UI (not part of the public pipelines API)
    for pipeline in design["pipelines"]:
        for stage in pipeline["stages"]:
            if stage["required_properties"]:
                add("Stage rules (set in UI)", "manual",
                    f"{pipeline['label']} / {stage['label']}: require {', '.join(stage['required_properties'])}")
    return items


def stage_body(stage, order):
    return {"label": stage["label"], "displayOrder": order,
            "metadata": {"probability": str(stage["probability"])}}


# ---------------------------------------------------------------- apply

def fill(value, ids):
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda m: ids[m.group(1)], value)
    if isinstance(value, list):
        return [fill(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, ids) for k, v in value.items()}
    return value


def apply(items, client, state, log=print):
    ids = {name: s["objectTypeId"] for name, s in state["schemas"].items()}
    errors = 0
    for item in items:
        if item["action"] != "create":
            continue
        method, path, body = item["request"]
        try:
            resp = client.request(method, fill(path, ids), body=fill(body, ids))
        except KeyError as missing:
            log(f"  skipped {item['desc']}: {missing} was not created")
            errors += 1
            continue
        except ApiError as err:
            if err.status == 409:
                log(f"  = {item['desc']} (already exists)")
                continue
            log(f"  ! {item['desc']}: {err}")
            errors += 1
            continue
        if item["registers"]:
            ids[item["registers"]] = resp.get("objectTypeId", resp.get("id"))
        log(f"  + {item['desc']}")
    return errors


SYMBOLS = {"create": "+", "exists": "=", "differs": "~", "manual": "!"}


def print_plan(items, log=print):
    section = None
    for item in items:
        if item["section"] != section:
            section = item["section"]
            log(f"\n{section}")
        log(f"  {SYMBOLS[item['action']]} {item['desc']}")
    counts = {a: sum(1 for i in items if i["action"] == a) for a in SYMBOLS}
    log(f"\nSummary: {counts['create']} to create, {counts['exists']} already there, "
        f"{counts['differs']} differ from config, {counts['manual']} stage rules to set in the UI")
    if counts["manual"]:
        log("Stage rules: Settings > Objects > Deals > Pipelines > (pipeline) > Edit stage properties.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = parser.parse_args(argv)

    design = load_design(args.config)
    client = hubspot_client(dry_run=args.dry_run)

    if args.dry_run and not client.live_reads:
        print("Dry run without HUBSPOT_TOKEN: no API calls, planning against an empty portal.")
        state = empty_state()
    else:
        print("Reading current portal setup...")
        state = read_state(client, design)

    items = plan(design, state)
    print_plan(items)
    if args.dry_run:
        print("\nDry run: nothing was changed.")
        return 0

    print("\nApplying...")
    errors = apply(items, client, state)
    print("Done." if not errors else f"Finished with {errors} error(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
