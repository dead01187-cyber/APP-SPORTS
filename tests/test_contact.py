from datetime import timedelta

from sqlalchemy import select

from leadflow.integrations.base import IntegrationError
from leadflow.integrations.claude import ClassificationError
from leadflow.models import DncEntry, Job, LeadStatus, Message, PhoneStatus, ReviewItem
from leadflow.pipeline.contact import handle_delivery_status, handle_inbound, is_opt_out, run_campaign
from leadflow.timeutil import add_months

from .conftest import DAYTIME, NIGHT


def contacted(db, services, make_lead, **overrides):
    lead = make_lead(**overrides)
    run_campaign(db, services, lead_ids=[lead.id], now=DAYTIME)
    db.commit()
    return lead


def jobs(db, type_=None):
    stmt = select(Job).where(Job.status == "pending")
    if type_:
        stmt = stmt.where(Job.type == type_)
    return db.scalars(stmt).all()


def test_campaign_sends_initial_sms_in_lead_language(db, services, make_lead):
    en = make_lead()
    es = make_lead(**{"Owner Name": "Maria Lopez", "Property Address": "9 Oak Ave", "Phone 1": "818-247-9000",
                      "Language": "es"})
    report = run_campaign(db, services, now=DAYTIME)
    assert report["sent"] == 2
    bodies = dict(services.sms.sent)
    assert bodies["+12134821234"].startswith("Hi John, this is Daniel")
    assert "123 Main Street" in bodies["+12134821234"]
    assert bodies["+18182479000"].startswith("Hola Maria, soy Daniel")
    assert en.status == es.status == LeadStatus.CONTACTED
    assert any("Initial SMS sent" in text for _, text in services.lofty.notes)


def test_no_texts_during_quiet_hours(db, services, make_lead):
    make_lead()
    report = run_campaign(db, services, now=NIGHT)
    assert report["skipped_reason"] == "quiet_hours"
    assert report["next_window"].startswith("2026-10-21T16:00")  # 9:00 Los Angeles
    assert services.sms.sent == []


def test_landline_is_skipped_for_the_next_mobile(db, services, make_lead):
    lead = make_lead(**{"Phone 2": "818-247-9000"})
    services.sms.line_types["+12134821234"] = "landline"
    run_campaign(db, services, now=DAYTIME)
    assert services.sms.sent[0][0] == "+18182479000"
    assert lead.phones[0].status == PhoneStatus.LANDLINE


def test_lead_with_only_dnc_numbers_is_not_texted(db, services, make_lead):
    lead = make_lead()
    db.add(DncEntry(phone="+12134821234"))
    db.commit()
    report = run_campaign(db, services, now=DAYTIME)
    assert report["no_phone"] == 1 and services.sms.sent == []
    assert lead.status == LeadStatus.NO_PHONE


def test_call_mode_builds_the_call_list(db, services, make_lead):
    lead = make_lead()
    report = run_campaign(db, services, mode="call", now=NIGHT)
    assert report["call_list"] == 1 and services.sms.sent == []
    item = db.scalars(select(ReviewItem)).one()
    assert item.kind == "call" and item.lead_id == lead.id and "+12134821234" in item.summary


def test_carrier_unsubscribe_error_adds_number_to_dnc(db, services, make_lead):
    lead = make_lead(**{"Phone 2": "818-247-9000"})
    services.sms.fail = IntegrationError("sms", "send", "HTTP 400", retryable=False, code=21610)
    run_campaign(db, services, now=DAYTIME)
    assert db.scalars(select(DncEntry.phone)).all() == ["+12134821234", "+18182479000"]
    assert lead.status == LeadStatus.NO_PHONE


def test_opt_out_keywords():
    keywords = ["STOP", "ALTO", "NO MAS", "UNSUBSCRIBE"]
    assert is_opt_out("stop", keywords)
    assert is_opt_out(" Stop. ", keywords)
    assert is_opt_out("STOP texting me", keywords)
    assert is_opt_out("No más", keywords)
    assert is_opt_out("alto!", keywords)
    assert not is_opt_out("Can you stop by tomorrow afternoon to see the house?", keywords)
    assert not is_opt_out("Yes interested", keywords)


def test_stop_reply_marks_dnc_without_calling_claude(db, services, make_lead):
    lead = contacted(db, services, make_lead)
    decision = handle_inbound(db, services, "(213) 482-1234", "STOP", "SMin1", now=DAYTIME)
    db.commit()
    assert decision["category"] == "opt_out" and decision["status"] == LeadStatus.DNC
    assert lead.status == LeadStatus.DNC and "DNC" in lead.tags
    assert db.scalars(select(DncEntry.phone)).all() == ["+12134821234"]
    assert services.classifier.calls == []
    assert jobs(db) == []


def test_interested_seller_becomes_hot_and_queues_cma(db, services, make_lead):
    lead = contacted(db, services, make_lead)
    services.classifier.will_return("interested_selling")
    decision = handle_inbound(db, services, "+12134821234", "Yes, what could I get for it?", "SMin1", now=DAYTIME)
    db.commit()
    assert decision["status"] == "hot" and decision["next_action"] == "cma" and decision["cma_queued"]
    assert lead.intent == "selling" and {"Hot", "Seller Interested"} <= set(lead.tags)
    assert [j.type for j in jobs(db)] == ["cma"]
    assert services.alerts.sent and "Hot seller" in services.alerts.sent[0]
    call = services.classifier.calls[0]
    assert call["history"][0]["direction"] == "agent"  # Claude sees the message we sent
    assert call["lead"]["property_address"] == "123 Main Street"


def test_future_follow_up_uses_the_timeframe_from_the_reply(db, services, make_lead):
    contacted(db, services, make_lead)
    services.classifier.will_return("future_follow_up", follow_up_months=3)
    decision = handle_inbound(db, services, "+12134821234", "Maybe after the holidays", now=DAYTIME)
    assert decision["status"] == "follow_up"
    assert jobs(db, "follow_up")[0].run_at == add_months(DAYTIME, 3)


def test_future_follow_up_on_a_specific_date(db, services, make_lead):
    contacted(db, services, make_lead)
    services.classifier.will_return("future_follow_up", follow_up_date="2027-03-15")
    handle_inbound(db, services, "+12134821234", "Text me March 15", now=DAYTIME)
    run_at = jobs(db, "follow_up")[0].run_at
    assert run_at.date().isoformat() == "2027-03-15" and run_at.hour == 17  # 10:00 PDT


def test_follow_up_defaults_to_six_months_and_not_interested_to_a_year(db, services, make_lead):
    contacted(db, services, make_lead)
    services.classifier.will_return("future_follow_up")
    handle_inbound(db, services, "+12134821234", "not now", now=DAYTIME)
    assert jobs(db, "follow_up")[0].run_at == add_months(DAYTIME, 6)
    services.classifier.will_return("not_interested")
    decision = handle_inbound(db, services, "+12134821234", "Not selling", now=DAYTIME)
    assert decision["status"] == "not_interested"
    pending = jobs(db, "follow_up")
    assert len(pending) == 1 and pending[0].run_at == add_months(DAYTIME, 12)


def test_wrong_number_moves_to_the_next_phone(db, services, make_lead):
    lead = contacted(db, services, make_lead, **{"Phone 2": "818-247-9000"})
    services.classifier.will_return("wrong_number")
    decision = handle_inbound(db, services, "+12134821234", "Wrong number", now=DAYTIME)
    assert decision["next_phone"] == "+18182479000"
    assert lead.phones[0].status == PhoneStatus.WRONG_NUMBER
    job = jobs(db, "initial_contact")[0]
    assert job.payload["phone_id"] == lead.phones[1].id


def test_wrong_number_with_no_other_phone(db, services, make_lead):
    lead = contacted(db, services, make_lead)
    services.classifier.will_return("wrong_number")
    handle_inbound(db, services, "+12134821234", "Who? wrong person", now=DAYTIME)
    assert lead.status == LeadStatus.NO_PHONE


def test_low_confidence_goes_to_manual_review(db, services, make_lead):
    lead = contacted(db, services, make_lead)
    services.classifier.will_return("interested_selling", confidence=0.4, suggested_reply="Happy to help!")
    decision = handle_inbound(db, services, "+12134821234", "👍", now=DAYTIME)
    db.commit()
    assert decision["category"] == "unclear" and decision["next_action"] == "review"
    assert lead.status == LeadStatus.CONTACTED
    item = db.scalars(select(ReviewItem)).one()
    assert item.kind == "reply_review" and item.payload["suggested_reply"] == "Happy to help!"


def test_classifier_failure_goes_to_review(db, services, make_lead):
    contacted(db, services, make_lead)
    services.classifier.queue.append(ClassificationError("refused"))
    decision = handle_inbound(db, services, "+12134821234", "hmm", now=DAYTIME)
    db.commit()
    assert decision["next_action"] == "review"
    assert db.scalars(select(ReviewItem)).one().kind == "reply_review"


def test_spanish_reply_switches_language_and_tags(db, services, make_lead):
    lead = contacted(db, services, make_lead)
    services.classifier.will_return("question", language="es")
    handle_inbound(db, services, "+12134821234", "¿Quién es usted?", now=DAYTIME)
    assert lead.language == "es" and "ES" in lead.tags and lead.status == "warm"


def test_unknown_sender_and_duplicate_webhooks(db, services, make_lead):
    contacted(db, services, make_lead)
    assert handle_inbound(db, services, "+13105551234", "hello?", now=DAYTIME)["next_action"] == "review"
    assert handle_inbound(db, services, "+13105559999", "STOP", now=DAYTIME)["category"] == "opt_out"
    assert db.scalars(select(DncEntry.phone)).all() == ["+13105559999"]
    services.classifier.will_return("question")
    handle_inbound(db, services, "+12134821234", "Who is this?", "SMdup", now=DAYTIME)
    db.commit()
    assert handle_inbound(db, services, "+12134821234", "Who is this?", "SMdup", now=DAYTIME) == {"duplicate": True}


def test_auto_reply_when_enabled(db, services, make_lead):
    services.rules.classification.auto_reply = True
    contacted(db, services, make_lead)
    services.classifier.will_return("question", suggested_reply="I'm Daniel, a local agent. Want a free valuation?")
    decision = handle_inbound(db, services, "+12134821234", "Who is this?", now=DAYTIME)
    assert decision["auto_replied"] is True
    assert services.sms.sent[-1][1].startswith("I'm Daniel")


def test_delivery_status_callbacks(db, services, make_lead):
    lead = contacted(db, services, make_lead, **{"Phone 2": "818-247-9000"})
    message = db.scalars(select(Message).where(Message.direction == "out")).one()
    handle_delivery_status(db, message.provider_id, "undelivered", "30006")
    assert lead.phones[0].status == PhoneStatus.INVALID
    handle_delivery_status(db, message.provider_id, "failed", "21610")
    assert db.scalars(select(DncEntry.phone)).all() == ["+12134821234"]
    assert handle_delivery_status(db, "nope", "sent", None) == {"found": False}


def test_follow_up_job_is_not_duplicated_on_repeat_replies(db, services, make_lead):
    contacted(db, services, make_lead)
    for _ in range(2):
        services.classifier.will_return("future_follow_up", follow_up_months=2)
        handle_inbound(db, services, "+12134821234", "later", now=DAYTIME + timedelta(minutes=1))
    assert len(jobs(db, "follow_up")) == 1


def test_opt_out_closes_open_call_list_items(db, services, make_lead):
    lead = make_lead()
    run_campaign(db, services, mode="call", now=DAYTIME)
    handle_inbound(db, services, "+12134821234", "STOP", now=DAYTIME)
    db.commit()
    item = db.scalars(select(ReviewItem).where(ReviewItem.lead_id == lead.id)).one()
    assert item.status == "resolved" and item.resolution == "opted out"


def test_reply_from_dnc_lead_goes_to_a_person_not_to_claude(db, services, make_lead):
    lead = contacted(db, services, make_lead)
    handle_inbound(db, services, "+12134821234", "STOP", now=DAYTIME)
    decision = handle_inbound(db, services, "+12134821234", "Actually, what is my house worth?", now=DAYTIME)
    db.commit()
    assert decision["next_action"] == "review" and lead.status == LeadStatus.DNC
    assert services.classifier.calls == []
    assert db.scalars(select(ReviewItem).where(ReviewItem.kind == "dnc_reply")).one().lead_id == lead.id
