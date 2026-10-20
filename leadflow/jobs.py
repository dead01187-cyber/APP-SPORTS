"""Job runner. Zapier (Schedule by Zapier) calls POST /jobs/run-due every 15-60 min.

Job types: follow_up, initial_contact, cma, cma_deliver, cma_follow_up, retry.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .integrations.base import IntegrationError
from .models import Cma, Job, Lead, LeadStatus
from .monitoring import log_event, open_review, retry_delay
from .pipeline import cma as cma_step
from .pipeline import contact
from .pipeline.prospecting import sync_to_lofty
from .timeutil import utcnow

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger("leadflow.jobs")

Handler = Callable[["Session", "Services", Job, datetime], str | None]
HANDLERS: dict[str, Handler] = {}
RETRY_TASKS: dict[str, Callable[["Session", "Services", dict, datetime], None]] = {}


class Reschedule(Exception):
    def __init__(self, run_at: datetime):
        super().__init__(run_at.isoformat())
        self.run_at = run_at


def handler(name: str):
    def wrap(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn
    return wrap


def retry_task(name: str):
    def wrap(fn):
        RETRY_TASKS[name] = fn
        return fn
    return wrap


def _lead(db: Session, lead_id: int | None) -> Lead:
    lead = db.get(Lead, lead_id) if lead_id else None
    if lead is None:
        raise ValueError(f"lead {lead_id} not found")
    return lead


def _require_window(services: Services, now: datetime) -> None:
    when = contact.reschedule_for_window(services, now)
    if when is not None:
        raise Reschedule(when)


# ---------------------------------------------------------------- handlers


@handler("follow_up")
def _follow_up(db: Session, services: Services, job: Job, now: datetime) -> str | None:
    lead = _lead(db, job.lead_id)
    if lead.status in (LeadStatus.DNC, LeadStatus.HOT, LeadStatus.NO_PHONE):
        return "cancelled"
    _require_window(services, now)
    message = contact.send_follow_up(db, services, lead)
    if message is None:
        lead.status = LeadStatus.NO_PHONE
        return "cancelled"
    return None


@handler("initial_contact")
def _initial_contact(db: Session, services: Services, job: Job, now: datetime) -> str | None:
    lead = _lead(db, job.lead_id)
    if lead.status == LeadStatus.DNC:
        return "cancelled"
    _require_window(services, now)
    message = contact.send_initial_to_phone(db, services, lead, job.payload.get("phone_id"))
    return None if message else "cancelled"


@handler("cma")
def _cma(db: Session, services: Services, job: Job, now: datetime) -> str | None:
    lead = _lead(db, job.lead_id)
    result = cma_step.run_for_lead(db, services, lead, now)
    return "skipped" if result.get("skipped") else None


@handler("cma_deliver")
def _cma_deliver(db: Session, services: Services, job: Job, now: datetime) -> str | None:
    lead = _lead(db, job.lead_id)
    cma = db.get(Cma, job.payload.get("cma_id"))
    if cma is None or cma.status == "sent" or lead.status == LeadStatus.DNC:
        return "cancelled"
    cma_step.deliver(db, services, lead, cma, now)
    return None


@handler("cma_follow_up")
def _cma_follow_up(db: Session, services: Services, job: Job, now: datetime) -> str | None:
    cma_step.cma_follow_up(db, services, _lead(db, job.lead_id))
    return None


@handler("retry")
def _retry(db: Session, services: Services, job: Job, now: datetime) -> str | None:
    task = job.payload.get("task")
    fn = RETRY_TASKS.get(task)
    if fn is None:
        raise ValueError(f"unknown retry task {task}")
    fn(db, services, job.payload, now)
    return None


# ---------------------------------------------------------------- retry tasks


@retry_task("lofty.sync_lead")
def _retry_sync(db: Session, services: Services, payload: dict, now: datetime) -> None:
    sync_to_lofty(db, services, _lead(db, payload.get("lead_id")), raise_errors=True)


@retry_task("lofty.note")
def _retry_note(db: Session, services: Services, payload: dict, now: datetime) -> None:
    lead = _lead(db, payload.get("lead_id"))
    if not lead.lofty_lead_id:
        sync_to_lofty(db, services, lead, raise_errors=True)
    services.lofty.add_note(lead.lofty_lead_id, payload.get("text", ""))
    services.lofty.upsert_lead(lead, services.rules.lofty)


@retry_task("contact.initial")
def _retry_initial(db: Session, services: Services, payload: dict, now: datetime) -> None:
    lead = _lead(db, payload.get("lead_id"))
    if lead.status != LeadStatus.IMPORTED:
        return
    _require_window(services, now)
    contact.verify_phones(db, services, lead)
    contact.send_initial(db, services, lead)


# ---------------------------------------------------------------- runner


def claim_due_jobs(db: Session, now: datetime, limit: int) -> list[int]:
    """Mark due jobs as running so two overlapping runs never process the same job."""
    stmt = (
        select(Job)
        .where(Job.status == "pending", Job.run_at <= now)
        .order_by(Job.run_at, Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    jobs = db.scalars(stmt).all()
    for job in jobs:
        job.status = "running"
        job.updated_at = now
    db.commit()
    return [j.id for j in jobs]


def fail_job(db: Session, services: Services, job: Job, error: str) -> None:
    job.status = "failed"
    job.last_error = error
    open_review(db, "failed_job", f"Job {job.type} #{job.id} failed after {job.attempts} attempt(s): {error}",
                job.lead_id, {"job_id": job.id, "type": job.type, "payload": job.payload})
    services.alerts.send(f"Leadflow: el trabajo {job.type} #{job.id} falló (lead {job.lead_id}): {error}")


def run_due_jobs(db: Session, services: Services, now: datetime | None = None, limit: int = 100) -> dict[str, Any]:
    now = now or utcnow()
    report: Counter = Counter()
    for job_id in claim_due_jobs(db, now, limit):
        job = db.get(Job, job_id)
        fn = HANDLERS.get(job.type)
        try:
            if fn is None:
                raise ValueError(f"unknown job type {job.type}")
            outcome = fn(db, services, job, now)
            job.status = {"cancelled": "cancelled", "skipped": "cancelled"}.get(outcome or "", "done")
            report[job.status] += 1
        except Reschedule as r:
            job.status = "pending"
            job.run_at = r.run_at
            report["rescheduled"] += 1
        except IntegrationError as exc:
            job.attempts += 1
            job.last_error = str(exc)
            log_event(db, "api_error", job.lead_id, service=exc.service, operation=exc.operation,
                      error=exc.message, job_id=job.id, attempt=job.attempts)
            if exc.retryable and job.attempts < job.max_attempts:
                job.status = "pending"
                job.run_at = now + timedelta(seconds=retry_delay(job.attempts))
                report["retrying"] += 1
            else:
                fail_job(db, services, job, str(exc))
                report["failed"] += 1
        except Exception as exc:  # unexpected bug: keep the queue moving, surface it for review
            log.exception("job %s failed", job_id)
            db.rollback()
            job = db.get(Job, job_id)
            job.attempts += 1
            fail_job(db, services, job, f"{type(exc).__name__}: {exc}")
            report["failed"] += 1
        db.commit()
    return {"processed": sum(report.values()), **report}
