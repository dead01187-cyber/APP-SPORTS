"""Step 3 · CMA to warm: choose warm/hot sellers -> filter -> create CMA in Lofty ->
send it by SMS with the link -> log in Lofty + schedule the next action."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations.base import IntegrationError
from ..models import Cma, Lead, LeadStatus
from ..monitoring import enqueue_job, log_event, open_review, resolve_reviews, retry_delay
from ..security import cma_form_url
from ..timeutil import utcnow
from .contact import lofty_note, next_send_time, render, send_to_first_usable, send_window_open, usable_phone

if TYPE_CHECKING:
    from ..services import Services

DONE_STATUSES = ("sent", "manual_pending", "needs_review")


def latest_cma(db: Session, lead: Lead) -> Cma | None:
    return db.scalars(select(Cma).where(Cma.lead_id == lead.id).order_by(Cma.id.desc())).first()


def eligibility(db: Session, services: Services, lead: Lead) -> str | None:
    """Return why the lead can't get a CMA yet, or None if it is eligible."""
    rules = services.rules.cma
    if lead.status == LeadStatus.DNC:
        return "on DNC list"
    if lead.status not in rules.eligible_statuses:
        return f"status is {lead.status}"
    if rules.sellers_only and lead.intent != "selling":
        return "not a seller"
    if not lead.property_address:
        return "no property address"
    if rules.language and lead.language != rules.language:
        return f"language is {lead.language}"
    if usable_phone(db, lead, services) is None:
        return "no valid phone"
    existing = latest_cma(db, lead)
    if existing and existing.status in DONE_STATUSES:
        return f"CMA already {existing.status}"
    return None


def validate(result: dict[str, Any], services: Services) -> list[str]:
    rules = services.rules.cma
    problems = []
    if not result.get("url"):
        problems.append("no link")
    if (result.get("comps") or 0) < rules.min_comps:
        problems.append(f"only {result.get('comps') or 0} comparables (minimum {rules.min_comps})")
    if (result.get("cost") or 0) > rules.max_cost:
        problems.append(f"cost {result.get('cost')} is above {rules.max_cost}")
    return problems


def run_for_lead(db: Session, services: Services, lead: Lead, now: datetime | None = None) -> dict[str, Any]:
    """Create and deliver a CMA. Raises IntegrationError on temporary failures so the job retries."""
    now = now or utcnow()
    existing = latest_cma(db, lead)
    if existing and existing.status == "ready" and existing.url:
        return deliver(db, services, lead, existing, now)

    reason = eligibility(db, services, lead)
    if reason:
        return {"lead_id": lead.id, "skipped": reason}
    if not services.rules.cma.lofty_api:
        return manual(db, services, lead, "CMA is made by hand (no Lofty CMA API)")

    try:
        result = services.lofty.create_cma(lead, services.rules.cma)
    except IntegrationError as exc:
        if exc.retryable:
            raise
        return manual(db, services, lead, exc.message)

    cma = Cma(
        lead_id=lead.id, lofty_cma_id=str(result.get("id") or "") or None, url=result.get("url"),
        comps_count=result.get("comps"), cost=result.get("cost"), status="ready",
    )
    db.add(cma)
    problems = validate(result, services)
    if problems:
        cma.status = "needs_review"
        open_review(db, "cma_review", f"Check CMA for {lead.property_address}: {', '.join(problems)}",
                    lead.id, {"url": cma.url, "problems": problems})
        log_event(db, "cma_needs_review", lead.id, problems=problems)
        return {"lead_id": lead.id, "needs_review": problems}
    db.flush()
    return deliver(db, services, lead, cma, now)


def manual(db: Session, services: Services, lead: Lead, reason: str) -> dict[str, Any]:
    """Ask the agent for the CMA: review item + alert with a signed link to paste it."""
    db.add(Cma(lead_id=lead.id, status="manual_pending"))
    radius = services.rules.cma.radius_miles
    form = cma_form_url(services.settings, lead.id)
    where = form or f"POST /cma/{lead.id}/link"
    open_review(
        db, "cma_manual",
        f"Create a CMA in Lofty for {lead.property_address} ({radius} mi radius), then send the link: {where}",
        lead.id, {"reason": reason, "form": form},
    )
    services.alerts.send(
        f"CMA pendiente: {lead.owner_name}, {lead.property_address}. Hágalo en Lofty ({radius:g} millas) "
        f"y pegue el link aquí para enviarlo por SMS: {where}"
    )
    log_event(db, "cma_manual", lead.id, reason=reason)
    return {"lead_id": lead.id, "manual": True, "reason": reason, "form": form}


def deliver(db: Session, services: Services, lead: Lead, cma: Cma, now: datetime | None = None) -> dict[str, Any]:
    """Text the CMA link (inside sending hours), log it in Lofty, schedule the follow-up call."""
    now = now or utcnow()
    if not send_window_open(services, now):
        cma.status = "ready"
        db.flush()
        when = next_send_time(services, now)
        enqueue_job(db, "cma_deliver", lead_id=lead.id, payload={"cma_id": cma.id}, run_at=when)
        return {"lead_id": lead.id, "scheduled_for": when.isoformat()}

    message = send_to_first_usable(db, services, lead, lambda _p: render(services, lead, "cma", link=cma.url), "cma")
    if message is None:
        cma.status = "needs_review"
        open_review(db, "cma_review", f"CMA ready for {lead.property_address} but no number can receive it",
                    lead.id, {"url": cma.url})
        return {"lead_id": lead.id, "needs_review": ["no textable phone"]}

    rules = services.rules.cma
    cma.status = "sent"
    cma.sent_at = now
    lead.add_tags(services.rules.tags.cma_sent)
    follow_up_at = now + timedelta(days=rules.follow_up_days)
    enqueue_job(db, "cma_follow_up", lead_id=lead.id, payload={"cma_id": cma.id}, run_at=follow_up_at)
    resolve_reviews(db, lead.id, ["cma_manual", "cma_review"], "CMA sent")
    lofty_note(db, services, lead,
               f"CMA sent by SMS to {message.phone}: {cma.url}\nNext action: call on {follow_up_at.date().isoformat()}.")
    log_event(db, "cma_sent", lead.id, url=cma.url, phone=message.phone)
    return {"lead_id": lead.id, "sent": True, "url": cma.url, "follow_up_at": follow_up_at.isoformat()}


def run_batch(
    db: Session, services: Services, limit: int = 25, lead_ids: list[int] | None = None, now: datetime | None = None
) -> dict[str, Any]:
    now = now or utcnow()
    stmt = select(Lead).where(Lead.status.in_(services.rules.cma.eligible_statuses))
    if lead_ids:
        stmt = stmt.where(Lead.id.in_(lead_ids))
    report: dict[str, Any] = {"sent": 0, "scheduled": 0, "manual": 0, "needs_review": 0, "skipped": 0, "retrying": 0}
    for lead in db.scalars(stmt.order_by(Lead.updated_at)).all():
        if report["sent"] + report["scheduled"] >= limit:
            break
        try:
            result = run_for_lead(db, services, lead, now)
        except IntegrationError as exc:
            log_event(db, "api_error", lead.id, service=exc.service, operation=exc.operation, error=exc.message)
            enqueue_job(db, "cma", lead_id=lead.id, run_at=now + timedelta(seconds=retry_delay(1)))
            report["retrying"] += 1
            continue
        if result.get("sent"):
            report["sent"] += 1
        elif result.get("scheduled_for"):
            report["scheduled"] += 1
        elif result.get("manual"):
            report["manual"] += 1
        elif result.get("needs_review"):
            report["needs_review"] += 1
        else:
            report["skipped"] += 1
    return report


def submit_link(db: Session, services: Services, lead: Lead, url: str, now: datetime | None = None) -> dict[str, Any]:
    """The agent made the CMA by hand: store the link and send it."""
    cma = latest_cma(db, lead)
    if cma is None or cma.status == "sent":
        cma = Cma(lead_id=lead.id)
        db.add(cma)
    cma.url = url
    cma.status = "ready"
    db.flush()
    return deliver(db, services, lead, cma, now)


def cma_follow_up(db: Session, services: Services, lead: Lead) -> None:
    if lead.status == LeadStatus.DNC:
        return
    open_review(db, "call", f"Call {lead.owner_name} about the CMA for {lead.property_address}", lead.id,
                {"reason": "cma_follow_up"})
    lofty_note(db, services, lead, "Reminder: call about the CMA that was sent.")
