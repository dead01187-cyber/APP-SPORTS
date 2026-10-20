from leadflow.normalize import canonicalize, make_dedupe_key, normalize_property_type, parse_csv, split_name
from leadflow.rules import Rules

from .conftest import row


def test_canonicalize_cleans_a_propwire_row():
    data = canonicalize(row(**{"Phone 2": "213-482-1234", "Phone 3": "555-1234"}))
    assert data["owner_name"] == "John Smith"
    assert (data["first_name"], data["last_name"]) == ("John", "Smith")
    assert data["property_address"] == "123 Main Street"
    assert data["city"] == "Los Angeles"
    assert data["state"] == "CA"
    assert data["zip_code"] == "90012"
    assert data["property_type"] == "single_family"
    assert data["email"] == "john@example.com"
    assert data["phones"] == ["+12134821234"]  # duplicate dropped
    assert data["invalid_phones"] == ["555-1234"]
    assert data["language"] == "en"


def test_dedupe_key_ignores_formatting():
    a = canonicalize(row())["dedupe_key"]
    b = canonicalize(row(**{"Property Address": "123 Main St.", "Owner Name": "John  Smith", "Zip": "90012"}))["dedupe_key"]
    assert a == b
    assert a != make_dedupe_key("125 Main St", "90012", "John Smith")


def test_company_owner_has_no_first_name():
    data = canonicalize(row(**{"Owner Name": "ACME HOLDINGS LLC"}))
    assert data["is_company"] is True
    assert data["first_name"] is None
    assert data["owner_name"] == "Acme Holdings LLC"


def test_split_name_variants():
    assert split_name("Smith, John") == ("John", "Smith")
    assert split_name("John & Jane Smith") == ("John", "Smith")
    assert split_name("Juan Y Maria Lopez") == ("Juan", "Lopez")
    assert split_name("Cher") == ("Cher", None)


def test_property_types():
    assert normalize_property_type("Multi-Family Residential") == "multi_family"
    assert normalize_property_type("Condominium") == "condo"
    assert normalize_property_type("SFR") == "single_family"
    assert normalize_property_type("Vacant Land") == "land"
    assert normalize_property_type("Barn") == "other"


def test_alternative_headers_and_spanish():
    data = canonicalize({"Owner 1 First Name": "MARIA", "Owner 1 Last Name": "LOPEZ", "Site Address": "9 Oak Ave",
                         "Zip Code": "91331", "Mobile": "8182479000", "Preferred Language": "Spanish"})
    assert data["owner_name"] == "Maria Lopez"
    assert data["first_name"] == "Maria"
    assert data["phones"] == ["+18182479000"]
    assert data["language"] == "es"


def test_parse_csv_handles_bom_and_blank_rows():
    text = "﻿Owner Name,Property Address,Zip,Phone 1\nJOHN SMITH,1 A St,90012,2134821234\n,,,\n"
    rows = parse_csv(text)
    assert len(rows) == 1
    assert canonicalize(rows[0])["owner_name"] == "John Smith"


def test_template_rendering_falls_back_when_name_missing():
    rules = Rules()
    en = rules.render("initial", "en", first_name=None, agent_name="Daniel", street="1 A St")
    es = rules.render("initial", "es", first_name=None, agent_name="Daniel", street="1 A St")
    assert en.startswith("Hi there, this is Daniel")
    assert es.startswith("Hola, soy Daniel")
    assert "STOP" in en and "STOP" in es


def test_cma_message_is_signed_with_agent_name_and_phone():
    from leadflow.config import Settings
    from leadflow.rules import load_rules

    from .conftest import ROOT

    rules = load_rules(ROOT / "rules.yaml")
    agent = Settings(_env_file=None)
    text = rules.render("cma", "es", first_name="Maria", street="9 Oak Ave", link="https://x/c",
                        agent_name=agent.agent_name, agent_phone=agent.agent_phone)
    assert text.endswith("Con gusto se lo explico. - Mark Rubio-Novack, (949) 534-3952")
    no_phone = rules.render("cma", "en", first_name="Ann", street="1 A St", link="https://x/c", agent_name="Mark")
    assert no_phone.endswith("Happy to walk you through it. - Mark")


def test_owner_type_tags_do_not_turn_a_person_into_a_company():
    person = canonicalize({"Owner 1 First Name": "JASON", "Owner 1 Last Name": "NAM", "Owner Type": "BANK,INVESTOR",
                           "Address": "1 A St", "Zip": "90012", "Phone 1": "2135550101"})
    assert person["is_company"] is False and person["first_name"] == "Jason"
    assert person["owner_type"] == "BANK,INVESTOR"  # kept for the CRM note


def test_mailing_address_without_state_or_zip():
    data = canonicalize(row(**{"Owner Mailing Address": "9 OTHER RD", "Owner Mailing City": "DOWNEY"}))
    assert data["mailing_address"] == "9 Other Rd, Downey"


def test_canonical_json_keys_from_zapier_still_map():
    data = canonicalize({"owner_first": "Maria", "owner_last": "Lopez", "property_address": "3 C St",
                         "zip_code": "90012", "phone1": "2135550101", "living_sqft": "1200", "ownership_months": "36"})
    assert data["owner_name"] == "Maria Lopez" and data["phones"] == ["+12135550101"]
    assert data["details"]["living_sqft"] == "1,200" and data["details"]["ownership_months"] == "36"


def test_entity_given_as_last_name_is_not_greeted():
    data = canonicalize({"first_name": "", "last_name": "SMITH FAMILY TRUST", "property_address": "1 A St"})
    assert data["owner_name"] == "Smith Family Trust"
    assert data["first_name"] is None and data["last_name"] is None


def test_unknown_do_not_call_markers_count_as_do_not_call():
    data = canonicalize({"owner_name": "Ann Lee", "property_address": "1 A St",
                         "phone1": "2135550101", "phone1_dnc": "Federal DNC",
                         "phone2": "2135550102", "phone2_dnc": "no"})
    assert data["dnc_phones"] == ["+12135550101"]


def test_condo_units_get_different_dedupe_keys_by_parcel():
    a = canonicalize(row(**{"APN": "100-001-01"}))["dedupe_key"]
    b = canonicalize(row(**{"APN": "100-001-02"}))["dedupe_key"]
    assert a != b
