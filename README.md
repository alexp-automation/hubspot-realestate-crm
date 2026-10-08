# HubSpot CRM for a real estate investment firm (Saudi Arabia)

A firm that sells land plots and leases offices, strip mall shops and apartments usually runs
two very different sales processes in one CRM, and it often shows: land buyers and tenants sit
in the same pipeline, nobody can say which plots are reserved or which units are vacant,
leases expire without anyone calling the tenant, and the finance team re-types closed deals
into Dynamics 365.

This repo is a **reference setup** I built to show how I would structure HubSpot for that kind
of business. It is a demo, not a copy of a client's system: all names, numbers and listings
are made up. Everything runs offline in dry-run mode, so you can read the plan before
anything touches a real portal.

What it sets up:

1. **Two custom objects.** *Property* (land plot, office building, strip mall, apartment
   building: city, district, plot area, zoning, deed number, asking price in SAR, status) and
   *Unit* (a leasable unit inside an income asset: floor, area, asking annual rent, status,
   lease end date, Ejar contract number).
2. **Three deal pipelines.** Land Sales (8 stages, from New inquiry to Due diligence & deed
   check), Leasing (8 stages, including Lease drafting on Ejar), and Renewals (5 stages,
   opened 90 days before a lease ends). Every stage has a probability, entry and exit criteria,
   and the fields a rep must fill before moving a deal there. Lost reason is mandatory on every
   lost stage.
3. **Lead capture.** A bilingual Arabic / English inquiry form and a Meta Lead Ads webhook.
   Phone numbers are cleaned to `+9665XXXXXXXX` (Arabic digits, `05...`, `00966...` all work),
   the language is detected, buyers go to Land Sales and tenants go to Leasing. Existing
   contacts are found by email, then by phone, so repeat inquiries don't create duplicates.
4. **Dynamics 365 sync.** Changed contacts and companies, and closed-won deals, are pushed to
   Dynamics `contact`, `account` and `opportunity` (marked Won). Each field has an owner, so
   finance data from Dynamics is never overwritten by HubSpot.

## What's in here

| File | What it is |
|---|---|
| `config/crm_design.json` | The whole CRM design in one file: objects, 51 custom properties, 3 pipelines with 21 stages, association labels, stage rules |
| `setup_hubspot.py` | Creates the design in a HubSpot portal through the CRM API. Reads what exists first and only creates what is missing |
| `lead_intake/server.py` | Webhook server for the website form and Meta Lead Ads |
| `lead_intake/leads.py` | Phone cleanup, Arabic detection, city / budget parsing, routing rules |
| `lead_intake/inquiry-form.html` | Drop-in inquiry form, Arabic (RTL) and English, honeypot field against bots |
| `dynamics_sync/field_mapping.json` | HubSpot to Dynamics field map, field owners and the conflict rule |
| `dynamics_sync/sync.py` | Pulls HubSpot changes since the last run and upserts them into Dataverse |
| `docs/architecture.md` | Data flow and pipeline diagrams, stage definitions, workflows and reports to build |
| `test/` | Unit tests (mocked HTTP), sample leads in English and Arabic, sample HubSpot records |

Plain Python 3.9+, standard library only. Nothing to install.

## Setup

**1. HubSpot.** Create a private app (Settings > Integrations > Private apps) with these scopes:
`crm.schemas.custom.read/write`, `crm.objects.custom.read/write`, `crm.objects.contacts.read/write`,
`crm.objects.companies.read/write`, `crm.objects.deals.read/write`, `crm.schemas.contacts.write`,
`crm.schemas.companies.write`, `crm.schemas.deals.write`. Custom objects need an Enterprise
portal (or a developer test account).

```bash
python3 setup_hubspot.py --dry-run                      # plan against an empty portal, no API calls
export HUBSPOT_TOKEN=pat-...
python3 setup_hubspot.py --dry-run                      # plan against your portal (reads only)
python3 setup_hubspot.py                                # create what is missing
```

The script prints `+` for things it will create, `=` for things already there and `~` for
things that exist but differ from the config (it reports those and leaves them alone). Run it
again after editing the config and it only adds the new parts. Rate limits (HTTP 429) are
retried with backoff.

HubSpot's API doesn't cover "required properties per stage", so the script ends with a
checklist of 18 stage rules to set in **Settings > Objects > Deals > Pipelines > Edit stage
properties**. The workflows for renewals and unit status are described in
`docs/architecture.md`.

**2. Lead intake.** Run the server anywhere with a public HTTPS URL (a small VPS, Render,
Railway, Cloud Run):

```bash
python3 lead_intake/server.py --port 8080
```

- Put `https://your-host/leads/website` into `ENDPOINT` in `inquiry-form.html`. Listing pages
  can link to the form with `?listing=JED-LND-0142` and the deal is linked to that Property.
- In the Meta app, subscribe the page to `leadgen` with callback `https://your-host/webhooks/meta`.
  Set `META_VERIFY_TOKEN` (subscription check) and `META_APP_SECRET` (signature check).
  Meta's webhook only carries the lead ID; the server expects the lead with `field_data`, as
  returned by the Graph API `/{lead-id}` call or forwarded by Zapier / Make / n8n.
- `ALLOWED_ORIGIN` limits which site can post the form (default `*`).

**3. Dynamics 365.** In Dataverse, add a text column `new_hubspotid` with an alternate key on
`contact`, `account` and `opportunity`, plus the `new_` columns listed in `field_mapping.json`.
Register an app in Entra ID, add it as an application user with a security role, then:

```bash
export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
export DYNAMICS_URL=https://yourorg.crm4.dynamics.com
python3 dynamics_sync/sync.py --dry-run --since 2026-10-01T00:00:00Z
python3 dynamics_sync/sync.py            # since last successful run
```

Run it every 15 minutes with cron. **n8n or Make can run it on a schedule instead**, or the same
logic can be rebuilt as an n8n workflow (HubSpot search node, Dataverse HTTP request with
the alternate key) if the team prefers a visual tool.

Conflict rule: every mapped field has an owner. HubSpot-owned fields (lead source, budget,
preferences) always go to Dynamics. Dynamics-owned fields (KYC status, ERP customer number,
VAT number, invoice status) are never pushed from HubSpot. Fields both teams edit (name, email,
CR number, Ejar number) go to whichever system changed the record last. Closed-won deals are
marked Won through the `WinOpportunity` action, because Dataverse doesn't allow setting that
state with a plain update.

## How to test

Everything below runs offline, without tokens:

```bash
python3 -m unittest discover -s test                                          # 47 tests
python3 setup_hubspot.py --dry-run                                            # full setup plan
python3 lead_intake/server.py --dry-run --file test/sample-leads.json         # 7 sample leads
python3 dynamics_sync/sync.py --dry-run --sample test/sample-hubspot-records.json --since 2026-10-01T00:00:00Z
```

The sample leads cover an English buyer from a listing page, an Arabic company looking for
office space (phone typed in Arabic digits), a lead with no intent field, Facebook and
Instagram lead ads, a bot caught by the honeypot, and a broken submission. Dry-run prints every
HubSpot call it would make.

To try the form locally, run the server with `--dry-run`, set `ENDPOINT` to
`http://localhost:8080/leads/website` and open `inquiry-form.html` in a browser.

## Easy to adapt

- **Other cities or asset types**: edit the options in `config/crm_design.json` and run the
  setup again; only the new options are reported.
- **More lead channels**: TikTok, Snapchat or Google lead forms, WhatsApp click-to-chat or a
  call tracking webhook can feed the same `leads.parse_*` and `route` functions.
- **Different routing**: brokers, a minimum budget, or per-city owners are a few lines in
  `leads.route`.
- **Another ERP**: the mapping file format works for Odoo, SAP Business One or NetSuite;
  only the upsert call in `sync.py` changes.
- **No code at all**: the config can be applied by hand from the dry-run output, and the intake
  and sync can be rebuilt in n8n or Make with the same rules.
