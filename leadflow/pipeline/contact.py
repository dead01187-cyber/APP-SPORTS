"""Step 2 · Contact: verify phone -> choose mode -> send initial message ->
process replies with Claude -> schedule follow-ups."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations.base import IntegrationError
from ..integrations.claude import Classification, ClassificationError
from ..integrations.sms import ERR_INVALID_NUMBERS, ERR_UNSUBSCRIBED, TEXTABLE_LINE_TYPES, UNDELIVERABLE_CODES
from ..models import DncEntry, Lead, LeadPhone, LeadStatus, Message, PhoneStatus
from ..monitoring import cancel_jobs, enqueue_job, log_event, open_review, record_failure, resolve_reviews
from ..normalize import normalize_phone
from ..timeutil import add_months, in_window, local_to_utc, next_window_start, to_local, utcnow

if TYPE_CHECKING:
    from ..services import Services


class ContactBlocked(Exception):
    """The lead or number must not be texted (DNC, opted out, invalid)."""


# ---------------------------------------------------------------- helpers


def is_dnc(db: Session, number: str) -> bool:
    return db.scalar(select(DncEntry.id).where(DncEntry.phone == number)) is not None


def add_dnc(db: Session, number: str, reason: str = "opt_out") -> None:
    if not is_dnc(db, number):
        db.add(DncEntry(phone=number, reason=reason))
        db.flush()
    for phone in db.scalars(select(LeadPhone).where(LeadPhone.number == number)):
        phone.status = PhoneStatus.DNC


def send_window_open(services: Services, now: datetime) -> bool:
    q = services.rules.contact.quiet_hours
    return in_window(now, q.start, q.end, q.timezone)


def next_send_time(services: Services, now: datetime) -> datetime:
    q = services.rules.contact.quiet_hours
    return next_window_start(now, q.start, q.end, q.timezone)


def usable_phone(db: Session, lead: Lead, services: Services, exclude: set[int] | None = None) -> LeadPhone | None:
    require_mobile = services.rules.contact.require_mobile
    for phone in lead.phones:
        if exclude and phone.id in exclude:
            continue
        if phone.status not in PhoneStatus.USABLE:
            continue
        if require_mobile and phone.line_type not in TEXTABLE_LINE_TYPES:
            continue
        if is_dnc(db, phone.number):
            phone.status = PhoneStatus.DNC
            continue
        return phone
    return None


def verify_phones(db: Session, services: Services, lead: Lead) -> None:
    """Check each unverified number (Twilio Lookup when enabled): valid? mobile?"""
    for phone in lead.phones:
        if phone.status != PhoneStatus.UNVERIFIED:
            continue
        if is_dnc(db, phone.number):
            phone.status = PhoneStatus.DNC
            continue
        try:
            result = services.sms.lookup(phone.number)
        except IntegrationError as exc:
            log_event(db, "api_error", lead.id, service=exc.service, operation=exc.operation, error=exc.message)
            continue
        phone.line_type = result.get("line_type")
        if not result.get("valid", True):
            phone.status = PhoneStatus.INVALID
        elif phone.line_type not in TEXTABLE_LINE_TYPES:
            phone.status = PhoneStatus.LANDLINE
        else:
            phone.status = PhoneStatus.VALID


def template_context(lead: Lead, services: Services, **extra: Any) -> dict[str, Any]:
    s = services.settings
    return {
        "first_name": lead.first_name,
        "owner_name": lead.owner_name,
        "street": lead.property_address,
        "city": lead.city,
        "agent_name": s.agent_name,
        "agent_phone": s.agent_phone,
        **extra,
    }


def render(services: Services, lead: Lead, name: str, **extra: Any) -> str:
    return services.rules.render(name, lead.language, **template_context(lead, services, **extra))


def status_callback_url(services: Services) -> str | None:
    base = services.settings.base_url
    return f"{base}/webhooks/sms/status" if base else None


def send_sms(db: Session, services: Services, lead: Lead, phone: LeadPhone, body: str, kind: str) -> Message:
    """Send one text. Raises ContactBlocked (do not retry) or IntegrationError (may retry)."""
    if lead.status == LeadStatus.DNC or phone.status == PhoneStatus.DNC or is_dnc(db, phone.number):
        raise ContactBlocked(f"{phone.number} is on the DNC list")
    try:
        sid = services.sms.send(phone.number, body, status_callback_url(services))
    except IntegrationError as exc:
        if exc.code == ERR_UNSUBSCRIBED:
            add_dnc(db, phone.number, "carrier_opt_out")
            log_event(db, "dnc_added", lead.id, phone=phone.number, reason="carrier_opt_out")
            raise ContactBlocked(f"{phone.number} unsubscribed") from exc
        if exc.code in ERR_INVALID_NUMBERS:
            phone.status = PhoneStatus.INVALID
            raise ContactBlocked(f"{phone.number} cannot receive SMS") from exc
        raise
    message = Message(
        lead_id=lead.id, direction="out", kind=kind, phone=phone.number, body=body,
        provider_id=sid or None, status="sent",
    )
    db.add(message)
    lead.last_contacted_at = utcnow()
    log_event(db, "sms_sent", lead.id, kind=kind, phone=phone.number)
    return message


def send_to_first_usable(db: Session, services: Services, lead: Lead, body_for: Any, kind: str) -> Message | None:
    """Try each usable phone in order until one accepts the text."""
    tried: set[int] = set()
    while True:
        phone = usable_phone(db, lead, services, exclude=tried)
        if phone is None:
            return None
        try:
            return send_sms(db, services, lead, phone, body_for(phone), kind)
        except ContactBlocked:
            tried.add(phone.id)


def lofty_note(db: Session, services: Services, lead: Lead, text: str) -> None:
    """Add a note to the lead in Lofty and push the current tags. Failures are retried later."""
    try:
        if not lead.lofty_lead_id:
            lead.lofty_lead_id = services.lofty.upsert_lead(lead, services.rules.lofty)
        services.lofty.add_note(lead.lofty_lead_id, text)
        services.lofty.upsert_lead(lead, services.rules.lofty)
    except IntegrationError as exc:
        record_failure(db, services, exc, task="lofty.note", payload={"lead_id": lead.id, "text": text}, lead_id=lead.id)


# ---------------------------------------------------------------- outreach


def run_campaign(
    db: Session,
    services: Services,
    mode: str | None = None,
    limit: int | None = None,
    lead_ids: list[int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Send the initial message to imported leads (SMS) or put them on the call list (call)."""
    now = now or utcnow()
    mode = mode or services.rules.contact.default_mode
    report: dict[str, Any] = {"mode": mode, "sent": 0, "call_list": 0, "no_phone": 0, "errors": 0, "lead_ids": []}
    if mode == "sms" and not send_window_open(services, now):
        report["skipped_reason"] = "quiet_hours"
        report["next_window"] = next_send_time(services, now).isoformat()
        return report

    stmt = select(Lead).where(Lead.status == LeadStatus.IMPORTED)
    if lead_ids:
        stmt = stmt.where(Lead.id.in_(lead_ids))
    leads = db.scalars(stmt.order_by(Lead.created_at, Lead.id).limit(limit or services.rules.contact.batch_limit)).all()

    for lead in leads:
        verify_phones(db, services, lead)
        if mode == "call":
            phone = next((p for p in lead.phones if p.status in PhoneStatus.USABLE | {PhoneStatus.LANDLINE}
                          and not is_dnc(db, p.number)), None)
            if phone is None:
                lead.status = LeadStatus.NO_PHONE
                report["no_phone"] += 1
                continue
            open_review(db, "call", f"Call {lead.owner_name} at {phone.number} about {lead.property_address}",
                        lead.id, {"phone": phone.number, "language": lead.language})
            lead.status = LeadStatus.CONTACTED
            lead.last_contacted_at = now
            report["call_list"] += 1
            report["lead_ids"].append(lead.id)
            lofty_note(db, services, lead, f"Added to call list ({phone.number}).")
            continue

        try:
            message = send_initial(db, services, lead)
        except IntegrationError as exc:
            record_failure(db, services, exc, task="contact.initial", payload={"lead_id": lead.id}, lead_id=lead.id)
            report["errors"] += 1
            continue
        if message is None:
            report["no_phone"] += 1
            continue
        report["sent"] += 1
        report["lead_ids"].append(lead.id)

    log_event(db, "campaign", None, **{k: v for k, v in report.items() if k != "lead_ids"})
    return report


def initial_template(lead: Lead) -> str:
    return "initial" if lead.contact_is_owner is not False else "initial_owner_check"


def send_initial(db: Session, services: Services, lead: Lead) -> Message | None:
    """First text to a lead, on the first number that can receive it. Raises IntegrationError."""
    message = send_to_first_usable(db, services, lead, lambda _p: render(services, lead, initial_template(lead)), "initial")
    if message is None:
        lead.status = LeadStatus.NO_PHONE
        return None
    lead.status = LeadStatus.CONTACTED
    lofty_note(db, services, lead, f"Initial SMS sent to {message.phone}: {message.body}")
    return message


def send_initial_to_phone(db: Session, services: Services, lead: Lead, phone_id: int | None) -> Message | None:
    """Used after a wrong-number reply: text the next number on file."""
    phone = db.get(LeadPhone, phone_id) if phone_id else usable_phone(db, lead, services)
    if phone is None or phone.lead_id != lead.id or phone.status not in PhoneStatus.USABLE:
        phone = usable_phone(db, lead, services)
    if phone is None:
        lead.status = LeadStatus.NO_PHONE
        return None
    try:
        message = send_sms(db, services, lead, phone, render(services, lead, initial_template(lead)), "initial")
    except ContactBlocked:
        return send_initial_to_phone(db, services, lead, None)
    lofty_note(db, services, lead, f"Initial SMS sent to next number {phone.number}.")
    return message


def send_follow_up(db: Session, services: Services, lead: Lead) -> Message | None:
    message = send_to_first_usable(db, services, lead, lambda _p: render(services, lead, "follow_up"), "follow_up")
    if message is None:
        return None
    lead.status = LeadStatus.CONTACTED
    lofty_note(db, services, lead, f"Follow-up SMS sent to {message.phone}: {message.body}")
    return message


# ---------------------------------------------------------------- replies


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).upper()
    return " ".join(re.sub(r"[^A-Z ]", " ", text).split())


def is_opt_out(body: str, keywords: list[str]) -> bool:
    """Carrier-style keyword check, done before Claude so opt-outs never depend on the model."""
    text = _fold(body)
    folded = {_fold(k) for k in keywords}
    if text in folded:
        return True
    words = text.split()
    return bool(words) and words[0] in {"STOP", "STOPALL", "UNSUBSCRIBE"} and len(words) <= 4


def follow_up_time(cls: Classification, services: Services, now: datetime, months: int | None = None) -> datetime:
    rules = services.rules.follow_up
    tz = services.rules.contact.quiet_hours.timezone
    if months is None and cls.follow_up_date:
        try:
            day = date.fromisoformat(cls.follow_up_date)
            local = to_local(now, tz).replace(
                year=day.year, month=day.month, day=day.day, hour=rules.send_hour, minute=0, second=0, microsecond=0
            )
            when = local_to_utc(local)
            if now < when <= add_months(now, rules.max_months):
                return when
        except ValueError:
            pass
    if months is None:
        months = cls.follow_up_months if cls.follow_up_months > 0 else rules.future_default_months
    return add_months(now, min(months, rules.max_months))


def lead_context(lead: Lead) -> dict[str, Any]:
    return {
        "owner_name": lead.owner_name,
        "property_address": lead.property_address,
        "city": lead.city,
        "current_status": lead.status,
        "preferred_language": lead.language,
        "first_text_asked_if_they_are_the_owner": "yes" if lead.contact_is_owner is False else "",
    }


def conversation(db: Session, lead: Lead, limit: int = 10) -> list[dict[str, str]]:
    rows = db.scalars(
        select(Message).where(Message.lead_id == lead.id).order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
    ).all()
    return [{"direction": "agent" if m.direction == "out" else "owner", "body": m.body} for m in reversed(rows)]


def handle_inbound(
    db: Session,
    services: Services,
    from_number: str,
    body: str,
    provider_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Process an incoming SMS: opt-out check, Claude classification, then the configured action."""
    now = now or utcnow()
    body = (body or "").strip()
    number = normalize_phone(from_number) or from_number

    if provider_id and db.scalar(
        select(Message.id).where(Message.provider_id == provider_id, Message.direction == "in")
    ):
        return {"duplicate": True}

    phone = db.scalars(select(LeadPhone).where(LeadPhone.number == number).order_by(LeadPhone.id.desc())).first()
    lead = phone.lead if phone else None
    history = conversation(db, lead) if lead else []
    message = Message(
        lead_id=lead.id if lead else None, direction="in", kind="reply", phone=number, body=body,
        provider_id=provider_id, status="received",
    )
    db.add(message)

    opted_out = is_opt_out(body, services.rules.contact.opt_out_keywords)
    if lead is None:
        if opted_out:
            add_dnc(db, number)
        log_event(db, "unknown_sender", None, phone=number, opted_out=opted_out)
        if not opted_out:
            open_review(db, "unknown_sender", f"Reply from unknown number {number}: “{body}”", None, {"phone": number})
        return {"lead_id": None, "category": "opt_out" if opted_out else None, "next_action": "stop" if opted_out else "review"}

    lead.last_reply_at = now
    if lead.status == LeadStatus.DNC and not opted_out:
        # Someone who opted out wrote again: a person decides whether to remove them from DNC.
        open_review(db, "dnc_reply", f"{lead.owner_name} (DNC) wrote: “{body}”", lead.id, {"phone": number})
        log_event(db, "dnc_reply", lead.id, phone=number)
        return {"lead_id": lead.id, "category": None, "next_action": "review", "status": lead.status}
    if opted_out:
        cls = Classification(category="opt_out", confidence=1.0, language=lead.language, summary="Opt-out keyword.")
    else:
        cls = classify_or_review(db, services, lead, body, history, now, channel="sms")
        if cls is None:
            return {"lead_id": lead.id, "category": None, "next_action": "review", "status": lead.status}
    return apply_classification(db, services, lead, phone, cls, message, body, now)


def handle_call_outcome(db: Session, services: Services, lead: Lead, notes: str, now: datetime | None = None) -> dict[str, Any]:
    """Classify the agent's notes from a phone call exactly like an SMS reply."""
    now = now or utcnow()
    history = conversation(db, lead)
    phone = usable_phone(db, lead, services) or (lead.phones[0] if lead.phones else None)
    message = Message(lead_id=lead.id, direction="in", channel="call", kind="call_notes",
                      phone=phone.number if phone else "", body=notes, status="received")
    db.add(message)
    lead.last_reply_at = now
    cls = classify_or_review(db, services, lead, notes, history, now, channel="call")
    if cls is None:
        return {"lead_id": lead.id, "category": None, "next_action": "review", "status": lead.status}
    return apply_classification(db, services, lead, phone, cls, message, notes, now)


def classify_or_review(
    db: Session, services: Services, lead: Lead, text: str, history: list[dict[str, str]], now: datetime, channel: str
) -> Classification | None:
    if not services.classifier.available:
        open_review(db, "reply_review", f"{lead.owner_name}: “{text}” (classifier not configured)", lead.id, {"text": text})
        return None
    try:
        context = {**lead_context(lead), "channel": channel}
        return services.classifier.classify(message=text, history=history, lead=context, today=now.date())
    except (IntegrationError, ClassificationError) as exc:
        log_event(db, "classification_failed", lead.id, error=str(exc))
        open_review(db, "reply_review", f"{lead.owner_name}: “{text}” (classification failed)", lead.id,
                    {"text": text, "error": str(exc)})
        return None


def apply_classification(
    db: Session,
    services: Services,
    lead: Lead,
    phone: LeadPhone | None,
    cls: Classification,
    message: Message,
    text: str,
    now: datetime,
) -> dict[str, Any]:
    rules = services.rules
    low_confidence = cls.confidence < rules.classification.min_confidence and cls.category != "opt_out"
    category = "unclear" if low_confidence else cls.category
    action = rules.action_for(category)

    message.category = cls.category
    message.confidence = cls.confidence
    lead.last_category = category
    if not low_confidence and cls.language != lead.language:
        lead.language = cls.language
    if lead.language == "es":
        lead.add_tags(rules.tags.spanish)
    if action.status != "unchanged":
        lead.status = action.status
        lead.add_tags(rules.tags.status.get(action.status))
    lead.add_tags(*action.tags)

    decision: dict[str, Any] = {
        "lead_id": lead.id,
        "lofty_lead_id": lead.lofty_lead_id,
        "category": category,
        "model_category": cls.category,
        "confidence": cls.confidence,
        "status": lead.status,
        "next_action": action.next,
        "summary": cls.summary,
    }

    if action.next == "cma":
        lead.intent = "selling"
        if rules.cma.auto_run:
            enqueue_job(db, "cma", lead_id=lead.id, run_at=now)
            decision["cma_queued"] = True
        services.alerts.send(f"Vendedor interesado: {lead.owner_name}, {lead.property_address}. Respondió: “{text}”")
    elif action.next == "notify_agent":
        if category == "interested_buying":
            lead.intent = lead.intent or "buying"
        open_review(db, "hot_lead", f"{lead.owner_name} ({category}): “{text}”", lead.id,
                    {"suggested_reply": cls.suggested_reply, "summary": cls.summary})
        services.alerts.send(f"Lead caliente ({category}): {lead.owner_name}. Respondió: “{text}”")
    elif action.next == "review":
        open_review(db, "reply_review", f"{lead.owner_name} ({category}): “{text}”", lead.id,
                    {"model_category": cls.category, "confidence": cls.confidence,
                     "suggested_reply": cls.suggested_reply, "summary": cls.summary})
    elif action.next in ("schedule", "schedule_long"):
        months = None if action.next == "schedule" else rules.follow_up.not_interested_months
        if months != 0:
            when = follow_up_time(cls, services, now, months)
            cancel_jobs(db, lead.id, ["follow_up"])
            enqueue_job(db, "follow_up", lead_id=lead.id, run_at=when)
            decision["follow_up_at"] = when.isoformat()
    elif action.next == "next_phone":
        if phone is not None:
            phone.status = PhoneStatus.WRONG_NUMBER
        nxt = usable_phone(db, lead, services)
        if nxt is not None:
            enqueue_job(db, "initial_contact", lead_id=lead.id, payload={"phone_id": nxt.id}, run_at=now)
            decision["next_phone"] = nxt.number
        else:
            lead.status = LeadStatus.NO_PHONE
            decision["status"] = lead.status
    elif action.next == "stop":
        if phone is not None:
            add_dnc(db, phone.number)
        lead.status = LeadStatus.DNC
        lead.add_tags(rules.tags.status.get("dnc"))
        cancel_jobs(db, lead.id)
        resolve_reviews(db, lead.id, ["call", "reply_review", "hot_lead", "cma_manual", "cma_review"], "opted out")
        log_event(db, "dnc_added", lead.id, phone=phone.number if phone else None, reason="opt_out")
        decision["status"] = lead.status

    if (
        rules.classification.auto_reply
        and not low_confidence
        and category in rules.classification.auto_reply_categories
        and cls.suggested_reply
        and phone is not None
        and message.channel == "sms"
    ):
        if send_window_open(services, now):
            try:
                send_sms(db, services, lead, phone, cls.suggested_reply, "auto_reply")
                decision["auto_replied"] = True
            except (ContactBlocked, IntegrationError) as exc:
                log_event(db, "auto_reply_failed", lead.id, error=str(exc))

    lofty_note(
        db, services, lead,
        f"{'Call notes' if message.channel == 'call' else 'SMS reply'}: “{text}”\n"
        f"Classified: {category} ({cls.confidence:.0%}). {cls.summary}\nNext action: {action.next}",
    )
    log_event(db, "reply_classified", lead.id, category=category, model_category=cls.category,
              confidence=cls.confidence, next_action=action.next)
    return decision


def handle_delivery_status(db: Session, provider_id: str, status: str, error_code: str | None) -> dict[str, Any]:
    """Twilio status callback: update the message; flag numbers that can't receive texts."""
    message = db.scalars(select(Message).where(Message.provider_id == provider_id, Message.direction == "out")).first()
    if message is None:
        return {"found": False}
    message.status = status
    if error_code:
        message.error = f"error {error_code}"
        phone = db.scalars(
            select(LeadPhone).where(LeadPhone.lead_id == message.lead_id, LeadPhone.number == message.phone)
        ).first()
        if str(error_code) == str(ERR_UNSUBSCRIBED):
            add_dnc(db, message.phone, "carrier_opt_out")
        elif str(error_code) in UNDELIVERABLE_CODES and phone is not None:
            phone.status = PhoneStatus.INVALID
        log_event(db, "sms_undelivered", message.lead_id, phone=message.phone, status=status, error_code=error_code)
    return {"found": True, "status": status}


def reschedule_for_window(services: Services, now: datetime) -> datetime | None:
    """None if we may text now, otherwise when the next sending window opens."""
    return None if send_window_open(services, now) else next_send_time(services, now)
