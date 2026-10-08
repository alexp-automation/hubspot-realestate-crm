"""Parsing, cleanup and routing of incoming leads. No network code here."""

import re
import unicodedata

# Arabic-Indic and Persian digits -> ASCII
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
ARABIC_LETTER = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
LATIN_LETTER = re.compile(r"[A-Za-z]")
INVISIBLE = re.compile(r"[​-‏‪-‮⁦-⁩﻿]")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

CITIES = {
    "riyadh": ["riyadh", "riyad", "الرياض", "رياض"],
    "jeddah": ["jeddah", "jedda", "jiddah", "جدة", "جده"],
    "dammam": ["dammam", "الدمام", "دمام"],
    "khobar": ["khobar", "al khobar", "alkhobar", "الخبر"],
    "makkah": ["makkah", "mecca", "makka", "مكة", "مكه"],
    "madinah": ["madinah", "medina", "madina", "المدينة", "المدينه"],
}
ASSET_TYPES = {
    "land": ["land", "plot", "أرض", "ارض", "قطعة", "أراضي", "اراضي"],
    "office": ["office", "مكتب", "مكاتب"],
    "retail": ["retail", "shop", "strip mall", "showroom", "store", "محل", "معرض", "تجاري"],
    "apartment_building": ["apartment", "flat", "building", "شقة", "شقق", "عمارة"],
}
BUY_WORDS = ["buy", "purchase", "invest", "sale", "for sale", "شراء", "أشتري", "اشتري", "شراء أرض", "استثمار", "بيع"]
RENT_WORDS = ["rent", "lease", "leasing", "إيجار", "ايجار", "استئجار", "أستأجر", "استاجر", "تأجير"]

SOURCES = {"facebook": "facebook_lead_ad", "fb": "facebook_lead_ad", "instagram": "instagram_lead_ad",
           "ig": "instagram_lead_ad"}


def clean(value):
    """Trim, drop invisible direction marks, collapse whitespace, unify Unicode."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = INVISIBLE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_phone(raw):
    """Return a Saudi number as +9665XXXXXXXX (landlines as +9661XXXXXXX).

    Accepts 05x, 5x, 9665x, 009665x, +966 5x with spaces/dashes, and Arabic digits.
    Foreign numbers written with + or 00 are kept in E.164 form. Returns None if invalid.
    """
    text = clean(raw).translate(DIGITS)
    if not text:
        return None
    international = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"\D", "", text)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("966"):
        national = digits[3:]
    elif international:
        return "+" + digits if 8 <= len(digits) <= 15 else None
    else:
        national = digits
    national = national.lstrip("0") if national.startswith("0") else national
    if len(national) == 9 and national.startswith("5"):
        return "+966" + national
    if len(national) == 9 and national[0] == "1":  # landline, e.g. 012 xxx xxxx -> +96612xxxxxxx
        return "+966" + national
    return None


def detect_language(*texts):
    """'ar' if most letters in the text are Arabic, otherwise 'en'."""
    joined = " ".join(clean(t) for t in texts if t)
    arabic = len(ARABIC_LETTER.findall(joined))
    latin = len(LATIN_LETTER.findall(joined))
    if arabic == 0:
        return "en"
    return "ar" if arabic >= latin * 0.5 else "en"


def has_word(text, word):
    """Arabic words match anywhere; English words must start a word ('lease' not in 'please')."""
    if word.isascii():
        return re.search(r"\b" + re.escape(word), text) is not None
    return word in text


def _match(text, table):
    lowered = clean(text).lower()
    if not lowered:
        return None
    for key, words in table.items():
        if lowered == key or any(has_word(lowered, w) for w in words):
            return key
    return None


def normalize_city(value):
    if not clean(value):
        return None
    return _match(value, CITIES) or "other"


def normalize_asset_type(value):
    return _match(value, ASSET_TYPES)


def normalize_intent(value):
    lowered = clean(value).lower()
    if not lowered:
        return None
    if lowered in ("buy", "rent"):
        return lowered
    if any(has_word(lowered, w) for w in RENT_WORDS):
        return "rent"
    if any(has_word(lowered, w) for w in BUY_WORDS):
        return "buy"
    return None


def parse_budget(value):
    """'2,500,000' / '2.5m' / '٢ مليون' / '150k' / '150 ألف' -> number of SAR, or None."""
    text = clean(value).translate(DIGITS).lower().replace(",", "").replace("٬", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group())
    if re.search(r"\d\s*m\b|million|مليون", text):
        number *= 1_000_000
    elif re.search(r"\d\s*k\b|thousand|ألف|الف", text):
        number *= 1_000
    return number


def budget_range(value, intent):
    """Map free-text or numeric budget to the budget_range_sar option for this intent."""
    text = clean(value)
    if text.startswith(("buy_", "rent_")):
        return text
    amount = parse_budget(text)
    if amount is None:
        return None
    if intent == "rent":
        for limit, key in ((100_000, "rent_under_100k"), (300_000, "rent_100k_300k"), (1_000_000, "rent_300k_1m")):
            if amount < limit:
                return key
        return "rent_1m_plus"
    for limit, key in ((1_000_000, "buy_under_1m"), (3_000_000, "buy_1m_3m"), (10_000_000, "buy_3m_10m")):
        if amount < limit:
            return key
    return "buy_10m_plus"


def split_name(full_name):
    parts = clean(full_name).split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _build(fields, source):
    """Common shape for every channel."""
    first, last = split_name(fields.get("name") or "")
    if fields.get("first_name"):
        first, last = clean(fields["first_name"]), clean(fields.get("last_name"))
    message = clean(fields.get("message"))
    intent = normalize_intent(fields.get("interest")) or normalize_intent(message)
    asset_type = (normalize_asset_type(fields.get("asset_type")) or normalize_asset_type(fields.get("interest"))
                  or normalize_asset_type(message))
    lead = {
        "first_name": first,
        "last_name": last,
        "email": clean(fields.get("email")).lower(),
        "phone_raw": clean(fields.get("phone")),
        "phone": normalize_phone(fields.get("phone")),
        "intent": intent,
        "asset_type": asset_type,
        "city": normalize_city(fields.get("city")),
        "budget_range": budget_range(fields.get("budget"), intent),
        "client_type": "company" if clean(fields.get("company")) else "individual",
        "company": clean(fields.get("company")),
        "message": message,
        "listing_id": clean(fields.get("listing_id")).upper(),
        "source": source,
        "honeypot": clean(fields.get("company_website")),
    }
    lang = clean(fields.get("lang")).lower()
    if lang not in ("ar", "en"):
        lang = detect_language(fields.get("name"), fields.get("first_name"), fields.get("interest"), message)
    lead["language"] = lang
    return lead


def parse_website(payload):
    return _build(payload, "website_form")


META_FIELDS = {
    "full_name": "name", "first_name": "first_name", "last_name": "last_name", "email": "email",
    "phone_number": "phone", "city": "city", "company_name": "company",
    "looking_for": "interest", "what_are_you_looking_for?": "interest", "interest": "interest",
    "property_type": "asset_type", "asset_type": "asset_type", "budget": "budget",
    "budget_sar": "budget", "message": "message", "comments": "message", "listing_id": "listing_id",
}


def parse_meta(payload):
    """Meta Lead Ads lead(s) -> list of leads.

    Accepts a single lead as returned by the Graph API (`{"id", "field_data": [...]}`)
    or a webhook body with entry[].changes[].value that already carries field_data
    (as forwarded by Zapier/Make/n8n after the Graph API lookup).
    """
    raw_leads = []
    if "field_data" in payload:
        raw_leads.append(payload)
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if "field_data" in value:
                raw_leads.append(value)

    leads = []
    for raw in raw_leads:
        fields = {}
        for item in raw.get("field_data", []):
            key = META_FIELDS.get(clean(item.get("name")).lower())
            values = item.get("values") or []
            if key and values:
                fields[key] = values[0]
        platform = clean(raw.get("platform")).lower()
        lead = _build(fields, SOURCES.get(platform, "facebook_lead_ad"))
        lead["meta_lead_id"] = clean(raw.get("id") or raw.get("leadgen_id"))
        leads.append(lead)
    return leads


def validate(lead):
    """List of problems (field names). Empty list means the lead is usable."""
    errors = []
    if not lead["first_name"]:
        errors.append("name")
    if lead["email"] and not EMAIL.match(lead["email"]):
        errors.append("email")
    if lead["phone_raw"] and not lead["phone"]:
        errors.append("phone")
    if not (lead["email"] and "email" not in errors) and not lead["phone"]:
        errors.append("contact")  # need at least one way to reach them
    return errors


def route(lead):
    """Which pipeline gets this lead. Returns (pipeline_key, reason)."""
    if lead["intent"] == "buy":
        return "land_sales", "asked to buy"
    if lead["intent"] == "rent":
        return "leasing", "asked to rent"
    if lead["asset_type"] == "land":
        return "land_sales", "interested in land"
    if lead["asset_type"] in ("office", "retail", "apartment_building"):
        return "leasing", f"interested in {lead['asset_type']} (income assets are leased)"
    return "land_sales", "no clear intent, default pipeline"
