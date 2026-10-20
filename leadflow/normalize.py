"""Clean & normalize lead rows (Propwire CSV/API rows or JSON from Zapier)."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Any

import phonenumbers

# Canonical field -> accepted column names (compared lowercase, letters/digits only).
FIELD_ALIASES: dict[str, list[str]] = {
    "owner_name": ["ownername", "owner1name", "owner1fullname", "ownerfullname", "owner", "fullname", "name"],
    "first_name": ["firstname", "owner1firstname", "ownerfirstname"],
    "last_name": ["lastname", "owner1lastname", "ownerlastname"],
    "property_address": [
        "propertyaddress", "address", "streetaddress", "siteaddress", "situsaddress",
        "propertystreetaddress", "propertystreet",
    ],
    "city": ["city", "propertycity", "sitecity"],
    "state": ["state", "propertystate", "sitestate"],
    "zip_code": ["zip", "zipcode", "propertyzip", "propertyzipcode", "sitezip", "postalcode"],
    "property_type": ["propertytype", "type", "landuse", "propertyusetype", "usetype"],
    "mailing_address": ["mailingaddress", "owneraddress", "mailaddress", "ownermailingaddress"],
    "email": ["email", "email1", "owneremail", "emailaddress"],
    "phone1": [
        "phone", "phone1", "phonenumber", "phonenumber1", "phone1number", "ownerphone",
        "mobile", "mobilephone", "cell", "cellphone", "wireless1",
    ],
    "phone2": ["phone2", "phonenumber2", "phone2number", "wireless2"],
    "phone3": ["phone3", "phonenumber3", "phone3number", "wireless3"],
    "external_id": ["id", "propertyid", "apn", "parcelid", "leadid", "externalid"],
    "language": ["language", "preferredlanguage", "idioma"],
}

_ALIAS_LOOKUP = {alias: field for field, aliases in FIELD_ALIASES.items() for alias in aliases}

COMPANY_RE = re.compile(
    r"\b(LLC|L\.?L\.?C|INC|INCORPORATED|CORP|CORPORATION|COMPANY|LP|LLP|LTD|TRUST|BANK|"
    r"ASSOCIATION|ASSN|HOLDINGS|PROPERTIES|INVESTMENTS?|PARTNERS|GROUP|CHURCH|ESTATE OF|"
    r"CITY OF|COUNTY OF|HOA)\b",
    re.IGNORECASE,
)

# Checked in order: the more specific types come before the generic "residential".
PROPERTY_TYPES = [
    ("condo", ("condo",)),
    ("townhouse", ("townhouse", "townhome", "town house")),
    ("multi_family", ("multi", "duplex", "triplex", "fourplex", "quadplex", "apartment")),
    ("mobile", ("mobile", "manufactured")),
    ("land", ("land", "lot", "vacant")),
    ("commercial", ("commercial", "office", "retail", "industrial")),
    ("single_family", ("single family", "single-family", "sfr", "residential")),
]

STREET_ABBREVIATIONS = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "boulevard": "blvd",
    "lane": "ln", "court": "ct", "place": "pl", "circle": "cir", "highway": "hwy",
    "parkway": "pkwy", "terrace": "ter", "trail": "trl", "north": "n", "south": "s",
    "east": "e", "west": "w", "apartment": "apt", "suite": "ste", "unit": "apt",
}


def header_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


KEEP_UPPER = {"LLC", "INC", "LP", "LLP", "LTD", "HOA", "PO", "NE", "NW", "SE", "SW", "II", "III", "IV"}


def smart_title(text: str | None) -> str | None:
    """Title-case text that arrived ALL CAPS or all lowercase; leave mixed case alone."""
    if not text:
        return text
    if text.isupper() or text.islower():
        words = []
        for w in text.split(" "):
            bare = re.sub(r"[^A-Za-z]", "", w).upper()
            words.append(w.upper() if bare in KEEP_UPPER else w.capitalize())
        return " ".join(words)
    return text


def normalize_phone(raw: Any, region: str = "US") -> str | None:
    text = clean_text(raw)
    if not text:
        return None
    try:
        parsed = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(raw: Any) -> str | None:
    text = clean_text(raw)
    if not text:
        return None
    text = text.lower()
    return text if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", text) else None


def normalize_zip(raw: Any) -> str | None:
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return None
    return digits[:5].zfill(5)


def normalize_property_type(raw: Any) -> str | None:
    text = (clean_text(raw) or "").lower()
    if not text:
        return None
    for canonical, keywords in PROPERTY_TYPES:
        if text == canonical or any(k in text for k in keywords):
            return canonical
    return "other"


def is_company_name(name: str | None) -> bool:
    return bool(name and COMPANY_RE.search(name))


def split_name(owner: str) -> tuple[str | None, str | None]:
    """'Smith, John' -> (John, Smith); 'John & Jane Smith' -> (John, Smith)."""
    if "," in owner:
        last, _, first = owner.partition(",")
        first_tokens = first.split()
        return (first_tokens[0] if first_tokens else None), (last.strip() or None)
    primary = re.split(r"\s+(?:&|and|y)\s+", owner, maxsplit=1, flags=re.IGNORECASE)[0]
    tokens = owner.split() if primary == owner else primary.split() + owner.split()[-1:]
    if len(tokens) == 1:
        return tokens[0], None
    return tokens[0], tokens[-1]


def address_key(address: str | None) -> str:
    if not address:
        return ""
    text = unicodedata.normalize("NFKD", address.lower())
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    words = [STREET_ABBREVIATIONS.get(w, w) for w in text.split()]
    return " ".join(words)


def name_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def make_dedupe_key(address: str | None, zip_code: str | None, owner: str | None) -> str:
    return f"{address_key(address)}|{zip_code or ''}|{name_key(owner)}"


def map_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map arbitrary column names onto canonical field names (first match wins)."""
    mapped: dict[str, Any] = {}
    for key, value in row.items():
        if key is None:
            continue
        canonical = key if key in FIELD_ALIASES or key == "phones" else _ALIAS_LOOKUP.get(header_key(key))
        if canonical and canonical not in mapped and value not in (None, ""):
            mapped[canonical] = value
    return mapped


def canonicalize(row: dict[str, Any]) -> dict[str, Any]:
    """Return a clean lead dict: names split, phones in E.164, address tidy, dedupe key."""
    m = map_row(row)

    first = smart_title(clean_text(m.get("first_name")))
    last = smart_title(clean_text(m.get("last_name")))
    owner = smart_title(clean_text(m.get("owner_name")))
    if not owner and (first or last):
        owner = " ".join(p for p in (first, last) if p)
    company = is_company_name(owner)
    if owner and not company and not (first or last):
        first, last = split_name(owner)
    if company:
        first = last = None

    raw_phones: list[Any] = []
    if isinstance(m.get("phones"), list):
        raw_phones.extend(m["phones"])
    raw_phones.extend(m.get(k) for k in ("phone1", "phone2", "phone3"))
    phones: list[str] = []
    invalid_phones: list[str] = []
    for raw in raw_phones:
        if raw in (None, ""):
            continue
        number = normalize_phone(raw)
        if number and number not in phones:
            phones.append(number)
        elif not number:
            invalid_phones.append(str(raw))

    language = (clean_text(m.get("language")) or "en").lower()
    language = "es" if language.startswith(("es", "sp")) else "en"

    address = smart_title(clean_text(m.get("property_address")))
    zip_code = normalize_zip(m.get("zip_code"))
    state = clean_text(m.get("state"))

    return {
        "external_id": clean_text(m.get("external_id")),
        "owner_name": owner,
        "first_name": first,
        "last_name": last,
        "is_company": company,
        "email": normalize_email(m.get("email")),
        "phones": phones,
        "invalid_phones": invalid_phones,
        "property_address": address,
        "city": smart_title(clean_text(m.get("city"))),
        "state": state.upper() if state and len(state) <= 3 else state,
        "zip_code": zip_code,
        "property_type": normalize_property_type(m.get("property_type")),
        "mailing_address": smart_title(clean_text(m.get("mailing_address"))),
        "language": language,
        "dedupe_key": make_dedupe_key(address, zip_code, owner),
    }


def parse_csv(text: str) -> list[dict[str, Any]]:
    """Parse a Propwire CSV export (any column order; headers matched by alias)."""
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))]
