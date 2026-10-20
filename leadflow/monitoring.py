"""Monitoring & error handling: audit log, review queue, retries, alerts, reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .integrations.base import IntegrationError
from .models import Event, Job, Lead, LeadStatus, ReviewItem
from .timeutil import utcnow

if TYPE_CHECKING:
    from .services import Services

# Seconds to wait before retry attempt 1, 2, 3, ...
RETRY_DELAYS = [60, 300, 1800, 7200, 43200]


def retry_delay(attempts: int) -> int:
    return RETRY_DELAYS[min(max(attempts, 1), len(RETRY_DELAYS)) - 1]


def log_event(db: Session, type_: str, lead_id: int | None = None, **data: Any) -> Event:
    event = Event(type=type_, lead_id=lead_id, data=_jsonable(data))
    db.add(event)
    return event


def open_review(
    db: Session, kind: str, summary: str, lead_id: int | None = None, payload: dict | None = None
) -> ReviewItem:
    """Add an item to the manual review queue (one open item per lead and kind)."""
    db.flush()
    if lead_id is not None:
        existing = db.scalars(
            select(ReviewItem).where(
                ReviewItem.kind == kind, ReviewItem.lead_id == lead_id, ReviewItem.status == "open"
            )
        ).first()
        if existing:
            existing.summary = summary
            existing.payload = _jsonable(payload or {})
            return existing
    item = ReviewItem(kind=kind, summary=summary, lead_id=lead_id, payload=_jsonable(payload or {}))
    db.add(item)
    return item


def resolve_reviews(db: Session, lead_id: int, kinds: list[str], resolution: str) -> None:
    for item in db.scalars(
        select(ReviewItem).where(
            ReviewItem.lead_id == lead_id, ReviewItem.kind.in_(kinds), ReviewItem.status == "open"
        )
    ):
        item.status = "resolved"
        item.resolution = resolution
        item.resolved_at = utcnow()


def enqueue_job(
    db: Session,
    type_: str,
    *,
    lead_id: int | None = None,
    payload: dict | None = None,
    run_at: datetime | None = None,
    max_attempts: int = 5,
) -> Job:
    job = Job(
        type=type_, lead_id=lead_id, payload=_jsonable(payload or {}),
        run_at=run_at or utcnow(), max_attempts=max_attempts,
    )
    db.add(job)
    return job


def cancel_jobs(db: Session, lead_id: int, types: list[str] | None = None) -> int:
    stmt = update(Job).where(Job.lead_id == lead_id, Job.status == "pending")
    if types:
        stmt = stmt.where(Job.type.in_(types))
    result = db.execute(stmt.values(status="cancelled", updated_at=utcnow()))
    return result.rowcount or 0


def record_failure(
    db: Session,
    services: Services,
    err: IntegrationError,
    *,
    task: str,
    payload: dict,
    lead_id: int | None = None,
) -> None:
    """Log an API error, then schedule a retry (temporary error) or open a review item + alert."""
    log_event(
        db, "api_error", lead_id, service=err.service, operation=err.operation,
        error=err.message, retryable=err.retryable, task=task,
    )
    if err.retryable:
        enqueue_job(
            db, "retry", lead_id=lead_id, payload={"task": task, **payload},
            run_at=utcnow() + timedelta(seconds=RETRY_DELAYS[0]),
        )
    else:
        open_review(db, "api_error", f"{err.service}.{err.operation} failed: {err.message}", lead_id, {"task": task, **payload})
        services.alerts.send(f"Leadflow: {err.service}.{err.operation} failed for lead {lead_id}: {err.message}")


def reconcile(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Find work that fell through the cracks (Zapier/Lofty out of sync) and requeue it."""
    now = now or utcnow()
    # Jobs stuck in "running" (process died mid-run) go back to pending.
    stale = db.execute(
        update(Job)
        .where(Job.status == "running", Job.updated_at < now - timedelta(minutes=30))
        .values(status="pending", updated_at=now)
    ).rowcount or 0

    pending_sync = set(
        db.scalars(
            select(Job.lead_id).where(Job.status.in_(["pending", "running"]), Job.type == "retry")
        )
    )
    unsynced = db.scalars(
        select(Lead).where(
            Lead.lofty_lead_id.is_(None),
            Lead.status == LeadStatus.NEW,
            Lead.created_at < now - timedelta(minutes=10),
        )
    ).all()
    requeued = 0
    for lead in unsynced:
        if lead.id not in pending_sync:
            enqueue_job(db, "retry", lead_id=lead.id, payload={"task": "lofty.sync_lead", "lead_id": lead.id}, run_at=now)
            requeued += 1
    failed_jobs = len(db.scalars(select(Job.id).where(Job.status == "failed")).all())
    log_event(db, "reconcile", stale_jobs_reset=stale, lofty_requeued=requeued, failed_jobs=failed_jobs)
    return {"stale_jobs_reset": stale, "lofty_requeued": requeued, "failed_jobs": failed_jobs}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
