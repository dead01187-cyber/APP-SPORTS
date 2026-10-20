"""Clean & normalize lead rows (Propwire CSV/API rows or JSON from Zapier)."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Any

import phonenumbers

# Canonical field -> accepted column names, compared lowercase with letters/digits only.
# Within each list the first column present wins (e.g. "Property Type" beats "Land Use").
FIELD_ALIASES: dict[str, list[str]] = {
    "owner_name": ["ownername", "owner1name", "owner1fullname", "ownerfullname", "owner", "fullname", "name"],
    "owner_first": ["owner1firstname", "ownerfirstname"],
    "owner_last": ["owner1lastname", "ownerlastname"],
    "owner2_first": ["owner2firstname"],
    "owner2_last": ["owner2lastname"],
    "owner3_first": ["owner3firstname"],
    "owner3_last": ["owner3lastname"],
    "owner4_first": ["owner4firstname"],
    "owner4_last": ["owner4lastname"],
    # The person a skip trace matched to the phones/emails (Propwire "Name 1").
    "contact_name": ["name1", "contactname", "skiptracename"],
    "first_name": ["firstname", "contactfirstname"],
    "last_name": ["lastname", "contactlastname"],
    "owner_type": ["ownertype"],
    "property_address": [
        "propertyaddress", "address", "streetaddress", "siteaddress", "situsaddress",
        "propertystreetaddress", "propertystreet",
    ],
    "city": ["city", "propertycity", "sitecity"],
    "state": ["state", "propertystate", "sitestate"],
    "zip_code": ["zip", "zipcode", "propertyzip", "propertyzipcode", "sitezip", "postalcode"],
    "property_type": ["propertytype", "propertyusetype", "usetype", "propertyuse", "landuse", "type"],
    "mailing_address": ["ownermailingaddress", "mailingaddress", "owneraddress", "mailaddress"],
    "mailing_city": ["ownermailingcity", "mailingcity", "mailcity"],
    "mailing_state": ["ownermailingstate", "mailingstate", "mailstate"],
    "mailing_zip": ["ownermailingzip", "mailingzip", "mailzip"],
    "email": ["email", "email1", "owneremail", "emailaddress"],
    "phone1": [
        "phone", "phone1", "phonenumber", "phonenumber1", "phone1number", "ownerphone",
        "mobile", "mobilephone", "cell", "cellphone", "wireless1",
    ],
    "phone2": ["phone2", "phonenumber2", "phone2number", "wireless2"],
    "phone3": ["phone3", "phonenumber3", "phone3number", "wireless3"],
    "phone1_type": ["phone1type", "phonetype1"],
    "phone2_type": ["phone2type", "phonetype2"],
    "phone3_type": ["phone3type", "phonetype3"],
    # Do Not Call flags per phone (Propwire: "Do Not 1 Call" = "dnc").
    "phone1_dnc": ["donot1call", "phone1dnc", "dnc1", "phone1donotcall"],
    "phone2_dnc": ["donot2call", "phone2dnc", "dnc2", "phone2donotcall"],
    "phone3_dnc": ["donot3call", "phone3dnc", "dnc3", "phone3donotcall"],
    # Known TCPA litigator (Propwire: "Litigator" = "tcpa").
    "litigator": ["litigator", "tcpalitigator"],
    "external_id": ["apn", "parcelid", "propertyid", "leadid", "externalid", "id"],
    "language": ["language", "preferredlanguage", "idioma"],
    "property_url": ["propertyurl"],
    "bedrooms": ["bedrooms", "beds"],
    "bathrooms": ["bathrooms", "baths"],
    "living_sqft": ["livingsquarefeet", "squarefeet", "sqft"],
    "year_built": ["yearbuilt"],
    "ownership_months": ["ownershiplengthmonths"],
    "vacant": ["vacant"],
}

COMPANY_RE = re.compile(
    r"\b(LLC|L\.?L\.?C|INC|INCORPORATED|CORP|CORPORATION|COMPANY|LP|LLP|LTD|BANK|ASSOCIATION|ASSN|"
    r"HOLDINGS|PROPERTIES|INVESTMENTS?|PARTNERS|GROUP|CHURCH|CITY OF|COUNTY OF|HOA)\b",
    re.IGNORECASE,
)
# Family/living trusts and estates: owned by people, but the name is not a person's name.
TRUST_RE = re.compile(r"\b(TRUST|TR|TRS|TRUSTEES?|REVOCABLE|IRREVOCABLE|ESTATE OF|LIVING)\b", re.IGNORECASE)

# A flag column counts as set unless it is empty or an explicit no. For Do Not Call and
# litigator columns, an unknown marker ("Federal DNC", "tcpa") must err on the safe side.
FALSE_FLAGS = {"", "no", "n", "false", "0", "0.0", "none", "null"}
# Words that say nothing about who a person is, ignored when matching contact and owner names.
NAME_NOISE = {
    "THE", "AND", "OF", "TRUST", "TR", "TRS", "TRUSTEE", "TRUSTEES", "FAMILY", "LIVING", "REVOCABLE",
    "IRREVOCABLE", "REV", "ESTATE", "SURVIVORS", "DECEDENTS", "SEPARATE", "PROPERTY", "DATED", "ETAL", "JR", "SR",
}

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


def make_dedupe_key(address: str | None, zip_code: str | None, owner: str | None, parcel: str | None = None) -> str:
    """One lead per property and owner. The parcel number (APN) tells condo units apart:
    Propwire leaves the unit out of "Address"."""
    key = f"{address_key(address)}|{zip_code or ''}|{name_key(owner)}"
    return f"{key}|{name_key(parcel)}" if parcel else key


def clean_number(value: Any, thousands: bool = False) -> str | None:
    """Propwire exports numbers like "3.000000000"; show 3, 2.5, 1,208. Zero or negative = unknown."""
    text = clean_text(value)
    if not text:
        return None
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return text
    if number <= 0:
        return None
    if number.is_integer():
        return f"{int(number):,}" if thousands else str(int(number))
    return f"{number:g}"


def is_flag(value: Any) -> bool:
    """Any marker other than an explicit no (Propwire writes "dnc", "tcpa", "1")."""
    return str(value if value is not None else "").strip().lower() not in FALSE_FLAGS


def name_words(*names: str | None) -> set[str]:
    words: set[str] = set()
    for name in names:
        for word in re.split(r"[^A-Za-z']+", name or ""):
            word = word.replace("'", "").upper()
            if len(word) >= 2 and word not in NAME_NOISE:
                words.add(word)
    return words


def normalize_line_type(raw: Any) -> str | None:
    text = re.sub(r"[^a-z]", "", str(raw or "").lower())
    if text in ("mobile", "cell", "cellular", "wireless"):
        return "mobile"
    if text in ("landline", "land", "fixed", "residential"):
        return "landline"
    if "voip" in text:
        return "nonFixedVoip"
    return None


def map_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map arbitrary column names onto canonical field names, by alias priority."""
    by_key: dict[str, Any] = {}
    for key, value in row.items():
        if key is None or value is None or (isinstance(value, str) and not value.strip()):
            continue
        by_key.setdefault(header_key(str(key)), value)
    mapped: dict[str, Any] = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in (header_key(field), *aliases):  # canonical JSON keys (owner_first, ...) always map
            if alias in by_key:
                mapped[field] = by_key[alias]
                break
    if isinstance(row.get("phones"), list):
        mapped["phones"] = row["phones"]
    return mapped


def _join(*parts: str | None) -> str | None:
    return " ".join(p for p in parts if p) or None


def canonicalize(row: dict[str, Any]) -> dict[str, Any]:
    """Return a clean lead dict: owner and contact names, phones in E.164 with their
    type and Do Not Call flag, tidy address, details for the CRM note, dedupe key."""
    m = map_row(row)

    owner_first = smart_title(clean_text(m.get("owner_first")))
    owner_last = smart_title(clean_text(m.get("owner_last")))
    first = smart_title(clean_text(m.get("first_name")))
    last = smart_title(clean_text(m.get("last_name")))
    owner = smart_title(clean_text(m.get("owner_name"))) or _join(owner_first, owner_last) or _join(first, last)
    # Propwire's "Owner Type" tags (BANK, INVESTOR, ...) also appear on individuals, so only
    # the name decides whether the owner is a business; the tags go into the CRM note.
    owner_type = (clean_text(m.get("owner_type")) or "").upper()
    company = is_company_name(owner)
    entity = company or bool(owner and TRUST_RE.search(owner))
    owner_from_parts = not (m.get("owner_name") or owner_first or owner_last)

    # Who we greet: the skip-traced contact, else explicit first/last, else the owner if a person.
    contact = smart_title(clean_text(m.get("contact_name")))
    if contact:
        first, last = split_name(contact)
    elif first or last:
        if entity and owner_from_parts:  # e.g. last_name "Smith Family Trust": not a person to greet
            first = last = None
    elif owner_first and not entity:
        first, last = owner_first, owner_last
    elif owner and not entity:
        first, last = split_name(owner)
    else:
        first = last = None

    entries: list[tuple[Any, Any, Any]] = []
    if isinstance(m.get("phones"), list):
        entries.extend((p, None, None) for p in m["phones"])
    entries.extend((m.get(f"phone{i}"), m.get(f"phone{i}_type"), m.get(f"phone{i}_dnc")) for i in (1, 2, 3))
    phones: list[str] = []
    phone_meta: dict[str, dict[str, Any]] = {}
    invalid_phones: list[str] = []
    for raw, line_type, dnc_flag in entries:
        if raw in (None, ""):
            continue
        number = normalize_phone(raw)
        if not number:
            invalid_phones.append(str(raw))
            continue
        if number in phone_meta:  # same number listed twice: keep the strictest flag
            phone_meta[number]["dnc"] = phone_meta[number]["dnc"] or is_flag(dnc_flag)
            continue
        phone_meta[number] = {"type": normalize_line_type(line_type), "dnc": is_flag(dnc_flag)}
        phones.append(number)

    language = (clean_text(m.get("language")) or "en").lower()
    language = "es" if language.startswith(("es", "sp")) else "en"

    address = smart_title(clean_text(m.get("property_address")))
    zip_code = normalize_zip(m.get("zip_code"))
    state = clean_text(m.get("state"))
    mailing = smart_title(clean_text(m.get("mailing_address")))
    if mailing:
        mailing_state = (clean_text(m.get("mailing_state")) or "").upper() or None
        parts = [mailing, smart_title(clean_text(m.get("mailing_city"))), _join(mailing_state, normalize_zip(m.get("mailing_zip")))]
        mailing = ", ".join(p for p in parts if p)

    # Skip traces often return an occupant rather than the owner; then the first text asks
    # whether they own the property instead of asking them to sell it.
    owner_words = name_words(owner, owner_first, owner_last, *(
        clean_text(m.get(f"owner{i}_{part}")) for i in (2, 3, 4) for part in ("first", "last")))
    contact_is_owner = not contact or not owner_words or bool(name_words(contact) & owner_words)

    return {
        "external_id": clean_text(m.get("external_id")),
        "owner_name": owner,
        "owner_type": owner_type or None,
        "contact_name": contact,
        "contact_is_owner": contact_is_owner,
        "first_name": first,
        "last_name": last,
        "is_company": company,
        "email": normalize_email(m.get("email")),
        "phones": phones,
        "phone_meta": phone_meta,
        "dnc_phones": [n for n in phones if phone_meta[n]["dnc"]],
        "invalid_phones": invalid_phones,
        "litigator": is_flag(m.get("litigator")),
        "property_address": address,
        "city": smart_title(clean_text(m.get("city"))),
        "state": state.upper() if state and len(state) <= 3 else state,
        "zip_code": zip_code,
        "property_type": normalize_property_type(m.get("property_type")),
        "mailing_address": mailing,
        "language": language,
        "details": {
            "property_type": clean_text(m.get("property_type")),
            "bedrooms": clean_number(m.get("bedrooms")),
            "bathrooms": clean_number(m.get("bathrooms")),
            "living_sqft": clean_number(m.get("living_sqft"), thousands=True),
            "year_built": clean_number(m.get("year_built")),
            "ownership_months": clean_number(m.get("ownership_months")),
            "vacant": is_flag(m.get("vacant")),
            "property_url": clean_text(m.get("property_url")),
        },
        "dedupe_key": make_dedupe_key(address, zip_code, owner, clean_text(m.get("external_id"))),
    }


def parse_csv(text: str) -> list[dict[str, Any]]:
    """Parse a Propwire CSV export (any column order; headers matched by alias)."""
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))]
