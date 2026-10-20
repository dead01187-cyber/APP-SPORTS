from datetime import timedelta

from sqlalchemy import select

from leadflow import jobs as jobs_mod
from leadflow.integrations.base import IntegrationError
from leadflow.jobs import run_due_jobs
from leadflow.models import Cma, Job, LeadStatus, ReviewItem
from leadflow.monitoring import enqueue_job, reconcile
from leadflow.pipeline import cma as cma_step
from leadflow.pipeline.contact import handle_inbound, run_campaign

from .conftest import DAYTIME, NIGHT


def hot_seller(db, services, make_lead, **overrides):
    lead = make_lead(**overrides)
    run_campaign(db, services, lead_ids=[lead.id], now=DAYTIME)
    services.classifier.will_return("interested_selling")
    handle_inbound(db, services, lead.phones[0].number, "Yes, send me the value", now=DAYTIME)
    db.commit()
    return lead


def reviews(db, kind):
    return db.scalars(select(ReviewItem).where(ReviewItem.kind == kind, ReviewItem.status == "open")).all()


def test_full_flow_from_reply_to_cma_and_follow_up_call(db, services, make_lead):
    lead = hot_seller(db, services, make_lead)
    report = run_due_jobs(db, services, now=DAYTIME)
    assert report["done"] == 1
    cma = db.scalars(select(Cma)).one()
    assert cma.status == "sent" and cma.url == "https://lofty.example/cma/1"
    assert services.sms.sent[-1][1].startswith("Hi John, here is your free market analysis")
    assert "https://lofty.example/cma/1" in services.sms.sent[-1][1]
    assert "CMA Sent" in lead.tags
    assert any("CMA sent by SMS" in text for _, text in services.lofty.notes)

    follow_up = db.scalars(select(Job).where(Job.type == "cma_follow_up")).one()
    assert follow_up.run_at == DAYTIME + timedelta(days=3)
    run_due_jobs(db, services, now=DAYTIME + timedelta(days=3))
    assert "about the CMA" in reviews(db, "call")[0].summary


def test_cma_not_available_in_lofty_goes_to_manual_then_link_is_sent(db, services, make_lead):
    lead = hot_seller(db, services, make_lead)
    services.lofty.cma_error = IntegrationError("lofty", "create_cma", "not available", retryable=False)
    run_due_jobs(db, services, now=DAYTIME)
    item = reviews(db, "cma_manual")[0]
    assert f"/cma/{lead.id}/link" in item.summary

    result = cma_step.submit_link(db, services, lead, "https://my.cma/abc", now=DAYTIME)
    db.commit()
    assert result["sent"] is True
    assert "https://my.cma/abc" in services.sms.sent[-1][1]
    assert reviews(db, "cma_manual") == []


def test_cma_with_too_few_comps_needs_review(db, services, make_lead):
    hot_seller(db, services, make_lead)
    services.lofty.cma_result = {"id": "c", "url": "https://x/c", "comps": 2, "cost": 0}
    sent_before = len(services.sms.sent)
    run_due_jobs(db, services, now=DAYTIME)
    assert db.scalars(select(Cma)).one().status == "needs_review"
    assert "only 2 comparables" in reviews(db, "cma_review")[0].summary
    assert len(services.sms.sent) == sent_before  # nothing texted until someone checks it


def test_cma_created_at_night_is_delivered_in_the_morning(db, services, make_lead):
    lead = hot_seller(db, services, make_lead)
    result = cma_step.run_for_lead(db, services, lead, now=NIGHT)
    db.commit()
    assert result["scheduled_for"].startswith("2026-10-21T16:00")
    sent_before = len(services.sms.sent)
    run_due_jobs(db, services, now=NIGHT + timedelta(hours=11))
    assert len(services.sms.sent) == sent_before + 1
    assert db.scalars(select(Cma)).one().status == "sent"


def test_cma_eligibility_rules(db, services, make_lead):
    lead = make_lead()
    assert cma_step.eligibility(db, services, lead) == "status is imported"
    lead.status, lead.intent = LeadStatus.WARM, None
    assert cma_step.eligibility(db, services, lead) == "not a seller"
    lead.intent = "selling"
    services.rules.cma.language = "es"
    assert cma_step.eligibility(db, services, lead) == "language is en"
    services.rules.cma.language = None
    assert cma_step.eligibility(db, services, lead) is None


def test_cma_batch_endpoint_logic(db, services, make_lead):
    services.rules.cma.auto_run = False
    hot_seller(db, services, make_lead)
    report = cma_step.run_batch(db, services, now=DAYTIME)
    assert report["sent"] == 1
    assert cma_step.run_batch(db, services, now=DAYTIME)["skipped"] == 1  # never sent twice


def test_follow_up_job_waits_for_sending_hours_then_sends(db, services, make_lead):
    lead = make_lead()
    lead.status = LeadStatus.FOLLOW_UP
    enqueue_job(db, "follow_up", lead_id=lead.id, run_at=NIGHT - timedelta(minutes=5))
    db.commit()
    assert run_due_jobs(db, services, now=NIGHT)["rescheduled"] == 1
    assert services.sms.sent == []
    run_due_jobs(db, services, now=NIGHT + timedelta(hours=11))
    assert services.sms.sent[0][1].startswith("Hi John, Daniel here following up")
    assert lead.status == LeadStatus.CONTACTED


def test_follow_up_is_cancelled_for_dnc_lead(db, services, make_lead):
    lead = make_lead()
    lead.status = LeadStatus.DNC
    enqueue_job(db, "follow_up", lead_id=lead.id, run_at=DAYTIME)
    db.commit()
    assert run_due_jobs(db, services, now=DAYTIME)["cancelled"] == 1
    assert services.sms.sent == []


def test_retry_backs_off_then_fails_into_review_and_alert(db, services, make_lead):
    services.lofty.fail_upsert = IntegrationError("lofty", "create_lead", "HTTP 503", retryable=True)
    lead = make_lead()
    job = db.scalars(select(Job)).one()
    now = job.run_at
    delays = []
    for _ in range(job.max_attempts):
        run_due_jobs(db, services, now=now)
        db.refresh(job)
        if job.status == "pending":
            delays.append(round((job.run_at - now).total_seconds()))
            now = job.run_at
    assert delays == [60, 300, 1800, 7200]
    assert job.status == "failed" and job.attempts == 5
    assert reviews(db, "failed_job")[0].lead_id == lead.id
    assert any("falló" in a for a in services.alerts.sent)

    services.lofty.fail_upsert = None  # Lofty is back: the next import works normally
    assert lead.lofty_lead_id is None


def test_retry_succeeds_once_lofty_recovers(db, services, make_lead):
    services.lofty.fail_upsert = IntegrationError("lofty", "create_lead", "HTTP 503", retryable=True)
    lead = make_lead()
    services.lofty.fail_upsert = None
    job = db.scalars(select(Job)).one()
    assert run_due_jobs(db, services, now=job.run_at)["done"] == 1
    assert lead.lofty_lead_id and lead.status == LeadStatus.IMPORTED


def test_unexpected_error_fails_the_job_but_keeps_the_queue_moving(db, services, make_lead, monkeypatch):
    lead = make_lead()
    enqueue_job(db, "cma_follow_up", lead_id=lead.id, run_at=DAYTIME)
    enqueue_job(db, "follow_up", lead_id=lead.id, run_at=DAYTIME)
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setitem(jobs_mod.HANDLERS, "cma_follow_up", boom)
    report = run_due_jobs(db, services, now=DAYTIME)
    assert report["failed"] == 1 and report["done"] == 1
    assert "RuntimeError: bug" in reviews(db, "failed_job")[0].summary


def test_reconcile_requeues_unsynced_leads_and_stuck_jobs(db, services, make_lead):
    services.lofty.fail_upsert = IntegrationError("lofty", "create_lead", "HTTP 401", retryable=False)
    make_lead()
    stuck = enqueue_job(db, "follow_up", lead_id=None, run_at=DAYTIME)
    db.commit()
    stuck.status = "running"
    stuck.updated_at = DAYTIME
    db.commit()
    result = reconcile(db, now=DAYTIME + timedelta(days=1))
    assert result["stale_jobs_reset"] == 1
    assert result["lofty_requeued"] == 1
