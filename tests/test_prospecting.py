from sqlalchemy import select

from leadflow.integrations.base import IntegrationError
from leadflow.models import DncEntry, Event, Job, Lead, LeadStatus, ReviewItem
from leadflow.pipeline.prospecting import ingest_rows

from .conftest import row


def test_import_creates_leads_and_syncs_to_lofty(db, services):
    report = ingest_rows(db, services, [row(), row(**{"Owner Name": "Maria Lopez", "Property Address": "9 Oak Ave",
                                                         "Phone 1": "818-247-9000", "Language": "es"})])
    db.commit()
    assert report.created == 2 and report.imported_to_lofty == 2
    leads = db.scalars(select(Lead).order_by(Lead.id)).all()
    assert all(lead.status == LeadStatus.IMPORTED and lead.lofty_lead_id for lead in leads)
    payload = services.lofty.payloads[leads[0].lofty_lead_id]
    assert payload["tags"] == ["Propwire"] and payload["source"] == "Propwire"
    assert payload["phones"] == ["2134821234"]  # Lofty format
    assert payload["cannotText"] is False and payload["cannotCall"] is False
    assert payload["streetAddress"] == "123 Main Street" and payload["zipCode"] == "90012"
    assert payload["leadTypes"] == [1]  # Seller
    assert payload["assignedUserId"] == 844771049678252  # Mark Rubio-Novack, from rules.yaml
    assert "pondId" not in payload

    services.rules.lofty.pond_id = 4242
    from leadflow.integrations.lofty import LoftyClient
    assert LoftyClient.lead_payload(leads[0], services.rules.lofty)["pondId"] == 4242
    assert "ES" in leads[1].tags and leads[1].language == "es"


def test_duplicates_are_skipped_in_batch_and_across_imports(db, services):
    first = ingest_rows(db, services, [row(), row(**{"Property Address": "123 Main St."})])
    db.commit()
    second = ingest_rows(db, services, [row()])
    assert (first.created, first.duplicates) == (1, 1)
    assert (second.created, second.duplicates) == (0, 1)


def test_rows_failing_filter_rules_are_rejected(db, services):
    services.rules.prospecting.property_types = ["single_family", "condo"]
    report = ingest_rows(db, services, [
        row(**{"Property Address": ""}),
        row(**{"Property Address": "2 B St", "Phone 1": "123"}),
        row(**{"Property Address": "3 C St", "Property Type": "Vacant Land"}),
    ])
    reasons = [r["reason"] for r in report.rejected]
    assert reasons == ["missing property_address", "no valid phone", "property type land not allowed"]
    assert report.created == 0


def test_enrichment_adds_phones_up_to_three_and_skips_dnc(db, services):
    db.add(DncEntry(phone="+13239382222"))
    db.commit()
    services.enrichment.results["John Smith"] = {
        "phones": ["323-938-2222", "818-247-9000", "(310) 555-1234", "213-555-0101"],
        "emails": ["other@example.com"],
        "relatives": ["Jane Smith"],
    }
    report = ingest_rows(db, services, [row()])
    lead = db.get(Lead, report.lead_ids[0])
    assert [p.number for p in lead.phones] == ["+12134821234", "+18182479000", "+13105551234"]
    assert [p.source for p in lead.phones] == ["propwire", "beenverified", "beenverified"]
    assert lead.relatives == ["Jane Smith"]
    assert lead.extra_emails == ["other@example.com"]


def test_lofty_outage_queues_a_retry(db, services):
    services.lofty.fail_upsert = IntegrationError("lofty", "create_lead", "HTTP 503", retryable=True)
    report = ingest_rows(db, services, [row()])
    db.commit()
    lead = db.get(Lead, report.lead_ids[0])
    assert lead.status == LeadStatus.NEW and lead.lofty_lead_id is None
    job = db.scalars(select(Job)).one()
    assert job.type == "retry" and job.payload == {"task": "lofty.sync_lead", "lead_id": lead.id}
    assert db.scalars(select(Event).where(Event.type == "api_error")).first() is not None


def test_permanent_lofty_error_goes_to_review_and_alerts(db, services):
    services.lofty.fail_upsert = IntegrationError("lofty", "create_lead", "HTTP 401", retryable=False)
    ingest_rows(db, services, [row()])
    db.commit()
    assert db.scalars(select(ReviewItem)).one().kind == "api_error"
    assert services.alerts.sent and "401" in services.alerts.sent[0]
    assert db.scalars(select(Job)).first() is None
