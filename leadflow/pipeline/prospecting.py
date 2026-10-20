"""Step 1 · Prospecting: get leads -> clean & normalize -> enrich -> filter & dedupe -> import to Lofty."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations.base import IntegrationError
from ..models import DncEntry, Lead, LeadPhone, LeadStatus, PhoneStatus
from ..monitoring import cancel_jobs, log_event, record_failure
from ..normalize import canonicalize, normalize_email, normalize_phone

if TYPE_CHECKING:
    from ..services import Services


@dataclass
class IngestReport:
    received: int = 0
    created: int = 0
    imported_to_lofty: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    lofty_errors: int = 0
    merged: int = 0
    existing_leads_protected: int = 0
    lead_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "created": self.created,
            "imported_to_lofty": self.imported_to_lofty,
            "duplicates": self.duplicates,
            "merged_same_contact": self.merged,
            "existing_leads_protected": self.existing_leads_protected,
            "rejected": len(self.rejected),
            "rejected_by_reason": dict(Counter(r["reason"] for r in self.rejected)),
            "rejected_rows": self.rejected[:50],
            "lofty_errors": self.lofty_errors,
            "lead_ids": self.lead_ids,
        }


def check_filters(data: dict[str, Any], services: Services) -> str | None:
    """Return the reason a lead is rejected, or None if it passes the filter rules."""
    rules = services.rules.prospecting
    for name in rules.required_fields:
        if not data.get(name):
            return f"missing {name}"
    if rules.skip_company_owners and data.get("is_company"):
        return "owner is a company"
    if rules.property_types and data.get("property_type") not in rules.property_types:
        return f"property type {data.get('property_type')} not allowed"
    if rules.require_phone and not data.get("phones"):
        return "no valid phone"
    return None


def enrich(data: dict[str, Any], services: Services, db: Session) -> None:
    """Add phones/emails/relatives from BeenVerified (skipped when not configured)."""
    if not services.rules.prospecting.enrich or not services.enrichment.available:
        return
    try:
        found = services.enrichment.lookup(data)
    except IntegrationError as exc:
        log_event(db, "api_error", None, service=exc.service, operation=exc.operation, error=exc.message,
                  owner=data.get("owner_name"))
        return
    for raw in found.get("phones", []):
        number = normalize_phone(raw)
        if number and number not in data["phones"]:
            data["phones"].append(number)
            data.setdefault("enriched_phones", []).append(number)
    emails = [e for e in (normalize_email(x) for x in found.get("emails", [])) if e]
    if emails and not data.get("email"):
        data["email"] = emails.pop(0)
    data["extra_emails"] = [e for e in emails if e != data.get("email")]
    data["relatives"] = found.get("relatives", [])


def ingest_rows(db: Session, services: Services, rows: list[dict[str, Any]], source: str = "propwire") -> IngestReport:
    report = IngestReport(received=len(rows))
    rules = services.rules.prospecting
    canonical = [canonicalize(row) for row in rows]
    dnc = set(db.scalars(select(DncEntry.phone)))

    # Pass 1: record every Do Not Call and litigator mark in the file before anything else, so a
    # flag protects rows that come earlier in the file, duplicates, and leads already in the system.
    new_dnc: dict[str, str] = {}
    for data in canonical:
        if data["litigator"] and rules.skip_litigators:
            for number in data["phones"]:
                if remember_dnc(db, number, "litigator", dnc):
                    new_dnc[number] = "litigator"
        if rules.skip_dnc_phones:
            for number in data["dnc_phones"]:
                if remember_dnc(db, number, "do_not_call_list", dnc):
                    new_dnc[number] = "do_not_call_list"
    report.existing_leads_protected = protect_existing_leads(db, services, new_dnc)

    # Pass 2: create leads.
    seen: set[str] = set()
    phone_owner: dict[str, Lead] = {}  # numbers given to a lead earlier in this file
    for index, (row, data) in enumerate(zip(rows, canonical, strict=True)):
        key = data["dedupe_key"]
        missing = next((f for f in rules.required_fields if not data.get(f)), None)
        if missing:
            report.rejected.append({"row": index, "reason": f"missing {missing}"})
            continue
        if key in seen or db.scalar(select(Lead.id).where(Lead.dedupe_key == key)):
            report.duplicates += 1
            continue
        seen.add(key)
        if data["litigator"] and rules.skip_litigators:
            report.rejected.append({"row": index, "reason": "TCPA litigator"})
            continue

        enrich(data, services, db)
        skipped_dnc = [p for p in data["phones"] if p in dnc]
        data["phones"] = [p for p in data["phones"] if p not in dnc]

        # The same contact behind several properties (condo units, a second home) gets one lead
        # and one text; the other property is added to that lead's notes.
        shared = {p: lead_for_phone(db, p, phone_owner) for p in data["phones"]}
        shared = {p: lead for p, lead in shared.items() if lead is not None}
        if data["phones"] and len(shared) == len(data["phones"]):
            target = shared[data["phones"][0]]
            add_linked_property(db, services, target, data)
            report.merged += 1
            continue
        data["phones"] = [p for p in data["phones"] if p not in shared][: rules.max_phones]

        reason = check_filters(data, services)
        if reason == "no valid phone" and skipped_dnc:
            reason = "all phones on Do Not Call"
        if reason:
            report.rejected.append({"row": index, "reason": reason})
            continue

        lead = Lead(
            dedupe_key=key,
            source=source,
            external_id=data["external_id"],
            owner_name=data["owner_name"],
            first_name=data["first_name"],
            last_name=data["last_name"],
            is_company=data["is_company"],
            contact_is_owner=data["contact_is_owner"],
            email=data["email"],
            extra_emails=data.get("extra_emails", []),
            relatives=data.get("relatives", []),
            property_address=data["property_address"],
            city=data["city"],
            state=data["state"],
            zip_code=data["zip_code"],
            property_type=data["property_type"],
            mailing_address=data["mailing_address"],
            language=data["language"],
            status=LeadStatus.NEW,
            tags=list(services.rules.lofty.tags),
            raw={
                **{k: v for k, v in row.items() if isinstance(k, str)},
                "leadflow_note": import_note(data, source, len(skipped_dnc)),
            },
        )
        if data["language"] == "es":
            lead.add_tags(services.rules.tags.spanish)
        if not data["contact_is_owner"]:
            lead.add_tags(services.rules.tags.contact_not_owner)
        enriched = set(data.get("enriched_phones", []))
        lead.phones = [
            new_phone(n, i, data["phone_meta"].get(n, {}).get("type"), "beenverified" if n in enriched else source)
            for i, n in enumerate(data["phones"])
        ]
        db.add(lead)
        db.flush()
        for number in data["phones"]:
            phone_owner[number] = lead
        report.created += 1
        report.lead_ids.append(lead.id)
        log_event(db, "lead_created", lead.id, source=source, phones=len(lead.phones))

        if sync_to_lofty(db, services, lead):
            report.imported_to_lofty += 1
        else:
            report.lofty_errors += 1

    log_event(db, "import_batch", None, **{k: v for k, v in report.as_dict().items() if k != "rejected_rows"})
    return report


def protect_existing_leads(db: Session, services: Services, new_dnc: dict[str, str]) -> int:
    """A number newly marked Do Not Call (or a litigator's) stops being used on leads already in the
    system: the phone is blocked, a litigator's lead becomes DNC, and Lofty is updated."""
    if not new_dnc:
        return 0
    affected: dict[int, Lead] = {}
    for phone in db.scalars(select(LeadPhone).where(LeadPhone.number.in_(list(new_dnc)))):
        phone.status = PhoneStatus.DNC
        lead = phone.lead
        affected[lead.id] = lead
        if new_dnc[phone.number] == "litigator" and lead.status != LeadStatus.DNC:
            lead.status = LeadStatus.DNC
            lead.add_tags(services.rules.tags.status.get("dnc"))
            cancel_jobs(db, lead.id)
    for lead in affected.values():
        log_event(db, "dnc_from_list", lead.id, numbers=[p.number for p in lead.phones if p.number in new_dnc])
        if lead.lofty_lead_id:
            sync_to_lofty(db, services, lead)
    return len(affected)


def lead_for_phone(db: Session, number: str, batch: dict[str, Lead]) -> Lead | None:
    if number in batch:
        return batch[number]
    phone = db.scalars(
        select(LeadPhone).where(LeadPhone.number == number, LeadPhone.status != PhoneStatus.WRONG_NUMBER)
        .order_by(LeadPhone.id)
    ).first()
    return phone.lead if phone else None


def add_linked_property(db: Session, services: Services, lead: Lead, data: dict[str, Any]) -> None:
    from .contact import lofty_note  # local import: contact imports nothing from this module

    d = data.get("details") or {}
    place = ", ".join(p for p in (data.get("property_address"), data.get("city")) if p)
    extra = " · ".join(p for p in (d.get("property_type"), data.get("external_id") and f"APN {data['external_id']}") if p)
    text = f"Same contact is also linked to: {place}" + (f" ({extra})" if extra else "")
    if data.get("owner_name") and data["owner_name"] != lead.owner_name:
        text += f". Owner of record: {data['owner_name']}"
    linked = list((lead.raw or {}).get("linked_properties", []))
    linked.append({"address": data.get("property_address"), "city": data.get("city"), "apn": data.get("external_id")})
    lead.raw = {**(lead.raw or {}), "linked_properties": linked}
    log_event(db, "property_linked", lead.id, address=data.get("property_address"))
    if lead.lofty_lead_id:
        lofty_note(db, services, lead, text)


def remember_dnc(db: Session, number: str, reason: str, known: set[str]) -> bool:
    """Add a number to the DNC table. Returns True if it was not there yet."""
    if number in known:
        return False
    db.add(DncEntry(phone=number, reason=reason))
    known.add(number)
    return True


def new_phone(number: str, position: int, line_type: str | None, source: str) -> LeadPhone:
    """A phone whose type the list already gives (mobile/landline) needs no paid lookup."""
    if line_type == "mobile":
        status = PhoneStatus.VALID
    elif line_type == "landline":
        status = PhoneStatus.LANDLINE
    else:
        status = PhoneStatus.UNVERIFIED
    return LeadPhone(number=number, position=position, status=status, line_type=line_type, source=source)


def import_note(data: dict[str, Any], source: str, skipped_dnc: int) -> str:
    """Property facts from the list, written once to the Lofty lead when it is created."""
    d = data.get("details") or {}
    owner = data.get("owner_name") or ""
    if data.get("owner_type"):
        owner += f" ({data['owner_type'].replace(',', ', ').lower()})"
    facts = [
        d.get("property_type"),
        d.get("bedrooms") and f"{d['bedrooms']} bd",
        d.get("bathrooms") and f"{d['bathrooms']} ba",
        d.get("living_sqft") and f"{d['living_sqft']} sqft",
        d.get("year_built") and f"built {d['year_built']}",
    ]
    lines = [
        f"Imported from {source.title()}.",
        f"Owner of record: {owner}" if owner else None,
        f"Contact found by skip trace: {data['contact_name']}"
        if data.get("contact_name") and data.get("contact_name") != data.get("owner_name") else None,
        "Property: " + " · ".join(f for f in facts if f) if any(facts) else None,
        f"Mailing address: {data['mailing_address']}" if data.get("mailing_address") else None,
        " · ".join(p for p in [
            data.get("external_id") and f"APN {data['external_id']}",
            d.get("ownership_months") and f"owned {d['ownership_months']} months",
            d.get("vacant") and "vacant",
        ] if p) or None,
        f"Phones left out (Do Not Call list): {skipped_dnc}" if skipped_dnc else None,
        d.get("property_url"),
    ]
    return "\n".join(line for line in lines if line)


def sync_to_lofty(db: Session, services: Services, lead: Lead, raise_errors: bool = False) -> bool:
    """Create/update the lead in Lofty. On failure, schedule a retry (or raise for the job runner)."""
    created = lead.lofty_lead_id is None
    try:
        lead.lofty_lead_id = services.lofty.upsert_lead(lead, services.rules.lofty)
    except IntegrationError as exc:
        if raise_errors:
            raise
        record_failure(db, services, exc, task="lofty.sync_lead", payload={"lead_id": lead.id}, lead_id=lead.id)
        return False
    if lead.status == LeadStatus.NEW:
        lead.status = LeadStatus.IMPORTED
    log_event(db, "lofty_synced", lead.id, lofty_lead_id=lead.lofty_lead_id)
    note = (lead.raw or {}).get("leadflow_note")
    if created and note:
        from .contact import lofty_note  # contact imports nothing from here; local import avoids a cycle risk

        lofty_note(db, services, lead, note)
    return True
