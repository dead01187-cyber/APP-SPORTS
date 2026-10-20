"""HTTP API. Zapier calls these endpoints (Webhooks by Zapier -> POST) and uses
the JSON responses to route the next step. Run: uvicorn leadflow.api:app"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Iterator
from typing import Any, Literal
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import jobs, monitoring, reports
from .config import Settings, get_settings
from .db import init_db, make_engine, make_sessionmaker
from .integrations.base import IntegrationError
from .models import Lead, ReviewItem
from .normalize import normalize_phone, parse_csv
from .pipeline import cma as cma_step
from .pipeline import contact
from .pipeline.prospecting import ingest_rows
from .services import Services, build_services
from .timeutil import utcnow

log = logging.getLogger("leadflow.api")


class ImportRequest(BaseModel):
    leads: list[dict[str, Any]]
    source: str = "propwire"


class CampaignRequest(BaseModel):
    mode: Literal["sms", "call"] | None = None
    limit: int | None = None
    lead_ids: list[int] | None = None


class CallOutcomeRequest(BaseModel):
    lead_id: int
    notes: str


class CmaRunRequest(BaseModel):
    limit: int = 25
    lead_ids: list[int] | None = None


class CmaLinkRequest(BaseModel):
    url: str


class JobsRequest(BaseModel):
    limit: int = 100


class ResolveRequest(BaseModel):
    resolution: str = "done"
    send_reply: str | None = None  # optional SMS to send to the lead (e.g. Claude's suggested reply)


class DncRequest(BaseModel):
    phone: str
    reason: str = "manual"


def lead_dict(lead: Lead) -> dict[str, Any]:
    return {
        "id": lead.id,
        "lofty_lead_id": lead.lofty_lead_id,
        "owner_name": lead.owner_name,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "is_company": lead.is_company,
        "email": lead.email,
        "property_address": lead.property_address,
        "city": lead.city,
        "state": lead.state,
        "zip_code": lead.zip_code,
        "property_type": lead.property_type,
        "language": lead.language,
        "status": lead.status,
        "intent": lead.intent,
        "last_category": lead.last_category,
        "tags": lead.tags,
        "phones": [
            {"number": p.number, "status": p.status, "line_type": p.line_type, "source": p.source} for p in lead.phones
        ],
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "last_contacted_at": lead.last_contacted_at.isoformat() if lead.last_contacted_at else None,
        "last_reply_at": lead.last_reply_at.isoformat() if lead.last_reply_at else None,
    }


def review_dict(item: ReviewItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "lead_id": item.lead_id,
        "summary": item.summary,
        "payload": item.payload,
        "created_at": item.created_at.isoformat(),
        "resolution": item.resolution,
    }


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_sessionmaker(engine)
    svc = services or build_services(settings)

    app = FastAPI(title="Leadflow", version="0.1.0", description="Propwire → BeenVerified → Lofty → SMS + Claude → CMA")
    app.state.services = svc
    app.state.session_factory = session_factory

    def get_db() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def check_key(x_api_key: str | None) -> None:
        if not settings.api_key:
            raise HTTPException(503, "API_KEY is not configured on the server")
        if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
            raise HTTPException(401, "invalid or missing X-API-Key")

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        check_key(x_api_key)

    async def read_webhook(request: Request) -> tuple[dict[str, Any], bool]:
        """Accept Twilio form posts (signed) or JSON from Zapier (X-API-Key). Returns (params, is_twilio)."""
        raw = await request.body()
        is_form = "application/x-www-form-urlencoded" in request.headers.get("content-type", "")
        try:
            params: dict[str, Any] = dict(parse_qsl(raw.decode("utf-8"))) if is_form else (await request.json() if raw else {})
        except ValueError as exc:
            raise HTTPException(422, "body must be form-encoded or JSON") from exc
        if not isinstance(params, dict):
            raise HTTPException(422, "body must be a JSON object")
        signature = request.headers.get("x-twilio-signature")
        if signature:
            url = str(request.url)
            if settings.public_base_url:
                url = settings.public_base_url.rstrip("/") + request.url.path
                if request.url.query:
                    url += "?" + request.url.query
            if not is_form or not svc.sms.validate_signature(url, params, signature):
                raise HTTPException(403, "invalid Twilio signature")
            return params, True
        check_key(request.headers.get("x-api-key"))
        return params, False

    @app.get("/health")
    def health(db: Session = Depends(get_db)) -> dict[str, Any]:
        return {"ok": True, **reports.health(db, svc)}

    # ---------------------------------------------------------- 1 · Prospecting

    @app.post("/prospecting/import", dependencies=[Depends(require_key)])
    def import_leads(body: ImportRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
        report = ingest_rows(db, svc, body.leads, body.source)
        db.commit()
        return report.as_dict()

    @app.post("/prospecting/import-csv", dependencies=[Depends(require_key)])
    async def import_csv(request: Request, source: str = "propwire", db: Session = Depends(get_db)) -> dict[str, Any]:
        text = (await request.body()).decode("utf-8-sig", errors="replace")
        report = ingest_rows(db, svc, parse_csv(text), source)
        db.commit()
        return report.as_dict()

    # ---------------------------------------------------------- 2 · Contact

    @app.post("/contact/campaign", dependencies=[Depends(require_key)])
    def campaign(body: CampaignRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
        result = contact.run_campaign(db, svc, body.mode, body.limit, body.lead_ids)
        db.commit()
        return result

    @app.post("/contact/call-outcome", dependencies=[Depends(require_key)])
    def call_outcome(body: CallOutcomeRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
        lead = db.get(Lead, body.lead_id)
        if lead is None:
            raise HTTPException(404, "lead not found")
        result = contact.handle_call_outcome(db, svc, lead, body.notes)
        monitoring.resolve_reviews(db, lead.id, ["call"], "call logged")
        db.commit()
        return result

    @app.post("/webhooks/sms/inbound")
    async def sms_inbound(request: Request, db: Session = Depends(get_db)) -> Any:
        params, is_twilio = await read_webhook(request)
        from_number = params.get("From") or params.get("from") or params.get("phone")
        body = params.get("Body") or params.get("body") or params.get("message") or ""
        provider_id = params.get("MessageSid") or params.get("message_id") or params.get("sid")
        if not from_number:
            raise HTTPException(422, "missing sender phone (From)")
        result = contact.handle_inbound(db, svc, from_number, body, provider_id)
        db.commit()
        if is_twilio:  # Twilio expects TwiML; an empty response means "send nothing back"
            return Response("<Response></Response>", media_type="application/xml")
        return result

    @app.post("/webhooks/sms/status")
    async def sms_status(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        params, _ = await read_webhook(request)
        sid = params.get("MessageSid") or params.get("message_id")
        status = params.get("MessageStatus") or params.get("status") or "unknown"
        if not sid:
            raise HTTPException(422, "missing MessageSid")
        result = contact.handle_delivery_status(db, sid, status, params.get("ErrorCode") or params.get("error_code"))
        db.commit()
        return result

    # ---------------------------------------------------------- 3 · CMA

    @app.post("/cma/run", dependencies=[Depends(require_key)])
    def cma_run(body: CmaRunRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
        result = cma_step.run_batch(db, svc, body.limit, body.lead_ids)
        db.commit()
        return result

    @app.post("/cma/{lead_id}/link", dependencies=[Depends(require_key)])
    def cma_link(lead_id: int, body: CmaLinkRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
        lead = db.get(Lead, lead_id)
        if lead is None:
            raise HTTPException(404, "lead not found")
        result = cma_step.submit_link(db, svc, lead, body.url)
        db.commit()
        return result

    # ---------------------------------------------------------- jobs, monitoring, review queue

    @app.post("/jobs/run-due", dependencies=[Depends(require_key)])
    def run_jobs(body: JobsRequest | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
        return jobs.run_due_jobs(db, svc, limit=(body.limit if body else 100))

    @app.post("/monitoring/reconcile", dependencies=[Depends(require_key)])
    def reconcile(db: Session = Depends(get_db)) -> dict[str, Any]:
        result = monitoring.reconcile(db)
        db.commit()
        return result

    @app.get("/review", dependencies=[Depends(require_key)])
    def review_list(status: str = "open", kind: str | None = None, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
        stmt = select(ReviewItem).where(ReviewItem.status == status)
        if kind:
            stmt = stmt.where(ReviewItem.kind == kind)
        return [review_dict(i) for i in db.scalars(stmt.order_by(ReviewItem.created_at.desc()).limit(200))]

    @app.post("/review/{item_id}/resolve", dependencies=[Depends(require_key)])
    def review_resolve(item_id: int, body: ResolveRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
        item = db.get(ReviewItem, item_id)
        if item is None:
            raise HTTPException(404, "review item not found")
        sent = False
        if body.send_reply and item.lead_id:
            lead = db.get(Lead, item.lead_id)
            try:
                message = contact.send_to_first_usable(db, svc, lead, lambda _p: body.send_reply, "manual_reply")
            except IntegrationError as exc:
                raise HTTPException(502, f"SMS failed: {exc.message}") from exc
            if message is None:
                raise HTTPException(409, "lead has no number that can receive SMS")
            sent = True
            contact.lofty_note(db, svc, lead, f"Manual SMS reply sent: {body.send_reply}")
        item.status = "resolved"
        item.resolution = body.resolution
        item.resolved_at = utcnow()
        db.commit()
        return {**review_dict(item), "reply_sent": sent}

    @app.post("/dnc", dependencies=[Depends(require_key)])
    def dnc_add(body: DncRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
        number = normalize_phone(body.phone)
        if not number:
            raise HTTPException(422, "invalid phone number")
        contact.add_dnc(db, number, body.reason)
        monitoring.log_event(db, "dnc_added", None, phone=number, reason=body.reason)
        db.commit()
        return {"phone": number, "dnc": True}

    # ---------------------------------------------------------- reports & lookups

    @app.get("/reports/summary", dependencies=[Depends(require_key)])
    def report_summary(days: int = 30, db: Session = Depends(get_db)) -> dict[str, Any]:
        return reports.summary(db, svc, days)

    @app.get("/leads/{lead_id}", dependencies=[Depends(require_key)])
    def get_lead(lead_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
        lead = db.get(Lead, lead_id)
        if lead is None:
            raise HTTPException(404, "lead not found")
        return lead_dict(lead)

    return app


def __getattr__(name: str) -> Any:
    # `uvicorn leadflow.api:app` builds the app lazily so tests can import this module freely.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
