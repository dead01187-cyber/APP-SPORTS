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
