"""Step 1 · Prospecting: get leads -> clean & normalize -> enrich -> filter & dedupe -> import to Lofty."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations.base import IntegrationError
from ..models import DncEntry, Lead, LeadPhone, LeadStatus, PhoneStatus
from ..monitoring import log_event, record_failure
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
    lead_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "created": self.created,
            "imported_to_lofty": self.imported_to_lofty,
            "duplicates": self.duplicates,
            "rejected": len(self.rejected),
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
    seen: set[str] = set()
    dnc = set(db.scalars(select(DncEntry.phone)))

    for index, row in enumerate(rows):
        data = canonicalize(row)
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
            # Never contact known TCPA litigators, now or if they show up again in another list.
            for number in data["phones"]:
                remember_dnc(db, number, "litigator", dnc)
            report.rejected.append({"row": index, "reason": "TCPA litigator"})
            continue
        if rules.skip_dnc_phones:
            for number in data["dnc_phones"]:
                remember_dnc(db, number, "do_not_call_list", dnc)

        enrich(data, services, db)
        skipped_dnc = [p for p in data["phones"] if p in dnc]
        data["phones"] = [p for p in data["phones"] if p not in dnc][: rules.max_phones]

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
        enriched = set(data.get("enriched_phones", []))
        lead.phones = [
            new_phone(n, i, data["phone_meta"].get(n, {}).get("type"), "beenverified" if n in enriched else source)
            for i, n in enumerate(data["phones"])
        ]
        db.add(lead)
        db.flush()
        report.created += 1
        report.lead_ids.append(lead.id)
        log_event(db, "lead_created", lead.id, source=source, phones=len(lead.phones))

        if sync_to_lofty(db, services, lead):
            report.imported_to_lofty += 1
        else:
            report.lofty_errors += 1

    log_event(db, "import_batch", None, **{k: v for k, v in report.as_dict().items() if k != "rejected_rows"})
    return report


def remember_dnc(db: Session, number: str, reason: str, known: set[str]) -> None:
    if number not in known:
        db.add(DncEntry(phone=number, reason=reason))
        known.add(number)


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
