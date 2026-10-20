"""The importer against Propwire's real export format (56 columns), with made-up people and numbers."""

from sqlalchemy import select

from leadflow.models import DncEntry, Lead
from leadflow.normalize import canonicalize, parse_csv
from leadflow.pipeline.contact import render, run_campaign, usable_phone
from leadflow.pipeline.prospecting import ingest_rows

from .conftest import DAYTIME, ROOT

SAMPLE = (ROOT / "tests" / "fixtures" / "propwire_sample.csv").read_text()


def rows():
    return parse_csv(SAMPLE)


def lead_at(db, address):
    return db.scalars(select(Lead).where(Lead.property_address == address)).one()


def test_columns_are_mapped_by_priority():
    maria = canonicalize(rows()[0])
    assert maria["property_type"] == "condo"  # "Property Type", not "Land Use: RESIDENTIAL"
    assert (maria["first_name"], maria["last_name"]) == ("Maria", "Lopez")
    assert maria["dnc_phones"] == ["+12135550102", "+12135550103"]
    assert maria["phone_meta"]["+12135550101"] == {"type": "mobile", "dnc": False}
    assert maria["mailing_address"] == "101 Example St, Los Angeles, CA 90012"
    assert maria["details"]["bathrooms"] == "2.5" and maria["details"]["living_sqft"] == "1,450"


def test_trust_owner_is_greeted_by_the_skip_traced_contact():
    trust = canonicalize(rows()[1])
    assert trust["owner_name"] == "John A Smith Living Trust"
    assert trust["contact_name"] == "Robert Smith"
    assert (trust["first_name"], trust["last_name"]) == ("Robert", "Smith")
    assert trust["is_company"] is False  # a family trust is not a business


def test_import_skips_dnc_numbers_and_litigators(db, services):
    report = ingest_rows(db, services, rows())
    db.commit()
    assert report.created == 3
    assert sorted(r["reason"] for r in report.rejected) == [
        "TCPA litigator", "all phones on Do Not Call", "no valid phone"]

    maria = lead_at(db, "101 Example St")
    assert [p.number for p in maria.phones] == ["+12135550101"]  # the two "dnc" numbers are gone
    dnc = dict(db.execute(select(DncEntry.phone, DncEntry.reason)).all())
    assert dnc["+12135550102"] == dnc["+12135550103"] == "do_not_call_list"
    assert dnc["+12135550108"] == dnc["+12135550109"] == "litigator"  # never texted if they come back

    payload = services.lofty.payloads[maria.lofty_lead_id]
    assert payload["phones"] == ["2135550101"]


def test_phone_type_from_the_list_decides_who_gets_a_text(db, services):
    ingest_rows(db, services, rows())
    db.commit()
    trust = lead_at(db, "202 Sample Ave")
    assert [(p.status, p.line_type) for p in trust.phones] == [("landline", "landline"), ("valid", "mobile")]
    assert usable_phone(db, trust, services).number == "+12135550105"

    run_campaign(db, services, now=DAYTIME)
    sent = dict(services.sms.sent)
    assert "+12135550104" not in sent  # landline: call list only
    assert sent["+12135550105"].startswith("Hi Robert, this is Daniel")
    assert sent["+12135550101"].startswith("Hi Maria, this is Daniel")


def test_lofty_note_carries_the_property_facts(db, services):
    ingest_rows(db, services, rows())
    db.commit()
    trust = lead_at(db, "202 Sample Ave")
    note = next(text for lofty_id, text in services.lofty.notes if lofty_id == trust.lofty_lead_id)
    assert "Owner of record: John A Smith Living Trust (individual, trust)" in note
    assert "Contact found by skip trace: Robert Smith" in note
    assert "Single Family Residence · 3 bd · 2 ba · 1,450 sqft · built 1962" in note
    assert "Mailing address: 9 Other Rd, Downey, CA 90241" in note
    assert "APN 100-002-02 · owned 240 months" in note

    maria = lead_at(db, "101 Example St")
    maria_note = next(text for lofty_id, text in services.lofty.notes if lofty_id == maria.lofty_lead_id)
    assert "Phones left out (Do Not Call list): 2" in maria_note
    assert "Contact found by skip trace" not in maria_note  # contact is the owner


def test_bank_owner_can_be_skipped(db, services):
    services.rules.prospecting.skip_company_owners = True
    report = ingest_rows(db, services, rows())
    assert "owner is a company" in [r["reason"] for r in report.rejected]
    assert not db.scalars(select(Lead).where(Lead.property_address == "505 Market St")).first()


def test_initial_text_in_spanish_uses_contact_name(db, services):
    ingest_rows(db, services, rows())
    trust = lead_at(db, "202 Sample Ave")
    trust.language = "es"
    assert render(services, trust, "initial").startswith("Hola Robert, soy Daniel")
