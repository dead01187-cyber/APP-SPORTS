"""Reports & analytics: leads imported, contacted, response rate, CMAs sent, hot leads, errors, health."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import distinct, func, select, text
from sqlalchemy.orm import Session

from .models import Cma, Event, Job, Lead, LeadStatus, Message, ReviewItem
from .timeutil import utcnow

if TYPE_CHECKING:
    from .services import Services


def summary(db: Session, services: Services, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    since = now - timedelta(days=days)

    def count(stmt) -> int:
        return int(db.scalar(stmt) or 0)

    imported = count(select(func.count(Lead.id)).where(Lead.created_at >= since, Lead.lofty_lead_id.is_not(None)))
    created = count(select(func.count(Lead.id)).where(Lead.created_at >= since))
    # SMS or call list, first contact or follow-up: any lead we reached out to in the period.
    contacted = count(select(func.count(Lead.id)).where(Lead.last_contacted_at >= since))
    replied = count(
        select(func.count(distinct(Message.lead_id))).where(
            Message.direction == "in", Message.lead_id.is_not(None), Message.created_at >= since
        )
    )
    cma_sent = count(select(func.count(Cma.id)).where(Cma.status == "sent", Cma.sent_at >= since))
    hot = count(select(func.count(Lead.id)).where(Lead.status == LeadStatus.HOT))
    errors_24h = count(
        select(func.count(Event.id)).where(Event.type == "api_error", Event.created_at >= now - timedelta(hours=24))
    )

    by_status = dict(db.execute(select(Lead.status, func.count(Lead.id)).group_by(Lead.status)).all())
    by_category = dict(
        db.execute(
            select(Message.category, func.count(Message.id))
            .where(Message.direction == "in", Message.category.is_not(None), Message.created_at >= since)
            .group_by(Message.category)
        ).all()
    )
    open_reviews = dict(
        db.execute(
            select(ReviewItem.kind, func.count(ReviewItem.id)).where(ReviewItem.status == "open").group_by(ReviewItem.kind)
        ).all()
    )
    jobs = dict(db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all())

    return {
        "period_days": days,
        "leads_created": created,
        "leads_imported": imported,
        "contacted": contacted,
        "replied": replied,
        "response_rate": round(replied / contacted, 3) if contacted else 0.0,
        "cma_sent": cma_sent,
        "opportunities_hot": hot,
        "leads_by_status": by_status,
        "replies_by_category": by_category,
        "errors_last_24h": errors_24h,
        "open_review_items": open_reviews,
        "jobs": jobs,
        "health": health(db, services),
    }


def health(db: Session, services: Services) -> dict[str, Any]:
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # pragma: no cover - reported, not raised
        database = f"error: {exc}"
    return {"database": database, "integrations": services.integration_status()}
