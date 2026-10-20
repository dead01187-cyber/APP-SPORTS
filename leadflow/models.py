"""Database tables: leads, phones, messages, jobs (follow-ups + retries), CMAs,
the DNC list, the audit log and the manual review queue."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .timeutil import utcnow


class LeadStatus:
    NEW = "new"  # saved, not yet in Lofty
    IMPORTED = "imported"  # in Lofty, ready for first contact
    CONTACTED = "contacted"  # message sent / on the call list, waiting for a reply
    HOT = "hot"
    WARM = "warm"
    FOLLOW_UP = "follow_up"
    NOT_INTERESTED = "not_interested"
    DNC = "dnc"
    NO_PHONE = "no_valid_phone"


class PhoneStatus:
    UNVERIFIED = "unverified"
    VALID = "valid"
    LANDLINE = "landline"
    INVALID = "invalid"
    WRONG_NUMBER = "wrong_number"
    DNC = "dnc"

    USABLE = {UNVERIFIED, VALID}


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(400), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(50), default="propwire")
    external_id: Mapped[str | None] = mapped_column(String(100))

    owner_name: Mapped[str] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    is_company: Mapped[bool] = mapped_column(Boolean, default=False)
    email: Mapped[str | None] = mapped_column(String(255))
    extra_emails: Mapped[list] = mapped_column(JSON, default=list)
    relatives: Mapped[list] = mapped_column(JSON, default=list)

    property_address: Mapped[str] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(20))
    zip_code: Mapped[str | None] = mapped_column(String(10))
    property_type: Mapped[str | None] = mapped_column(String(50))
    mailing_address: Mapped[str | None] = mapped_column(String(255))

    language: Mapped[str] = mapped_column(String(5), default="en")
    status: Mapped[str] = mapped_column(String(30), default=LeadStatus.NEW, index=True)
    intent: Mapped[str | None] = mapped_column(String(20))  # selling | buying
    last_category: Mapped[str | None] = mapped_column(String(40))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # Lofty lead IDs are 64-bit integers; kept as text so nothing overflows.
    lofty_lead_id: Mapped[str | None] = mapped_column(String(40), index=True)

    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_reply_at: Mapped[datetime | None] = mapped_column(DateTime)

    phones: Mapped[list[LeadPhone]] = relationship(
        back_populates="lead", order_by="LeadPhone.position", cascade="all, delete-orphan"
    )

    def add_tags(self, *tags: str | None) -> None:
        merged = list(self.tags or []) + [t for t in tags if t]
        self.tags = list(dict.fromkeys(merged))


class LeadPhone(Base):
    __tablename__ = "lead_phones"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    number: Mapped[str] = mapped_column(String(20), index=True)  # E.164
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default=PhoneStatus.UNVERIFIED)
    line_type: Mapped[str | None] = mapped_column(String(30))
    source: Mapped[str] = mapped_column(String(30), default="propwire")

    lead: Mapped[Lead] = relationship(back_populates="phones")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # out | in
    channel: Mapped[str] = mapped_column(String(10), default="sms")  # sms | call
    kind: Mapped[str] = mapped_column(String(20))  # initial | follow_up | cma | reply | ...
    phone: Mapped[str] = mapped_column(String(20), index=True)
    body: Mapped[str] = mapped_column(Text)
    provider_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="sent")
    category: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Job(Base):
    """Scheduled work: follow-ups, CMA runs and retries of failed API calls."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Cma(Base):
    __tablename__ = "cmas"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    # ready | sent | manual_pending | needs_review
    status: Mapped[str] = mapped_column(String(20), default="ready")
    lofty_cma_id: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(String(500))
    comps_count: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class DncEntry(Base):
    __tablename__ = "dnc"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(50), default="opt_out")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    """Audit log: every import, message, classification, API error, etc."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    lead_id: Mapped[int | None] = mapped_column(Integer, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ReviewItem(Base):
    """Manual review queue: unclear replies, questions, call list, failed tasks."""

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    lead_id: Mapped[int | None] = mapped_column(Integer, index=True)
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class SchedulerRun(Base):
    """One row per scheduled task run; the unique key stops two servers running the same slot."""

    __tablename__ = "scheduler_runs"
    __table_args__ = (UniqueConstraint("task", "slot", name="uq_scheduler_task_slot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task: Mapped[str] = mapped_column(String(40))
    slot: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
