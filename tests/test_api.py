import base64
import hashlib
import hmac
from datetime import time

import pytest
from fastapi.testclient import TestClient

from leadflow.api import create_app

KEY = {"X-API-Key": "test-key"}

CSV = (
    "Owner Name,Property Address,City,State,Zip,Property Type,Phone 1\n"
    "JOHN SMITH,123 MAIN STREET,LOS ANGELES,CA,90012,Single Family,(213) 482-1234\n"
    "MARIA LOPEZ,9 OAK AVE,PACOIMA,CA,91331,Condo,818-247-9000\n"
)


@pytest.fixture
def client(settings, services):
    services.rules.contact.quiet_hours.start = time(0, 0)
    services.rules.contact.quiet_hours.end = time(23, 59, 59)
    app = create_app(settings, services)
    return TestClient(app)


def twilio_signature(token, url, params):
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def test_health_is_public_but_everything_else_needs_the_key(client):
    assert client.get("/health").json()["ok"] is True
    assert client.post("/prospecting/import", json={"leads": []}).status_code == 401
    assert client.post("/prospecting/import", json={"leads": []}, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/reports/summary").status_code == 401


def test_end_to_end_over_http(client, services):
    imported = client.post("/prospecting/import-csv", content=CSV, headers={**KEY, "Content-Type": "text/csv"}).json()
    assert imported["created"] == 2 and imported["imported_to_lofty"] == 2

    again = client.post("/prospecting/import", json={"leads": [{"owner_name": "John Smith",
                        "property_address": "123 Main St", "zip_code": "90012", "phone1": "2134821234"}]},
                        headers=KEY).json()
    assert again["duplicates"] == 1

    sent = client.post("/contact/campaign", json={"mode": "sms"}, headers=KEY).json()
    assert sent["sent"] == 2

    services.classifier.will_return("interested_selling")
    decision = client.post("/webhooks/sms/inbound", json={"from": "+12134821234", "body": "Yes!", "message_id": "SMx"},
                           headers=KEY).json()
    assert decision["status"] == "hot" and decision["cma_queued"] is True

    ran = client.post("/jobs/run-due", headers=KEY).json()
    assert ran["done"] == 1
    assert "market analysis" in services.sms.sent[-1][1]

    summary = client.get("/reports/summary", headers=KEY).json()
    assert summary["leads_imported"] == 2
    assert summary["contacted"] == 2 and summary["replied"] == 1 and summary["response_rate"] == 0.5
    assert summary["cma_sent"] == 1 and summary["opportunities_hot"] == 1
    assert summary["replies_by_category"] == {"interested_selling": 1}
    assert summary["health"]["database"] == "ok"

    lead = client.get(f"/leads/{decision['lead_id']}", headers=KEY).json()
    assert lead["status"] == "hot" and "CMA Sent" in lead["tags"]


def test_twilio_webhook_signature(client, services, settings):
    client.post("/prospecting/import-csv", content=CSV, headers=KEY)
    client.post("/contact/campaign", json={}, headers=KEY)
    params = {"From": "+12134821234", "Body": "STOP", "MessageSid": "SM123"}
    url = "http://testserver/webhooks/sms/inbound"

    bad = client.post("/webhooks/sms/inbound", data=params, headers={"X-Twilio-Signature": "nope"})
    assert bad.status_code == 403

    good = client.post("/webhooks/sms/inbound", data=params,
                       headers={"X-Twilio-Signature": twilio_signature(settings.twilio_auth_token, url, params)})
    assert good.status_code == 200 and good.text == "<Response></Response>"
    assert client.get("/leads/1", headers=KEY).json()["status"] == "dnc"


def test_review_queue_and_manual_reply(client, services):
    client.post("/prospecting/import-csv", content=CSV, headers=KEY)
    client.post("/contact/campaign", json={}, headers=KEY)
    services.classifier.will_return("question", suggested_reply="I'm Daniel, a local agent.")
    client.post("/webhooks/sms/inbound", json={"from": "+12134821234", "body": "Who is this?"}, headers=KEY)

    items = client.get("/review", headers=KEY).json()
    assert len(items) == 1 and items[0]["payload"]["suggested_reply"] == "I'm Daniel, a local agent."

    resolved = client.post(f"/review/{items[0]['id']}/resolve",
                           json={"resolution": "replied", "send_reply": items[0]["payload"]["suggested_reply"]},
                           headers=KEY).json()
    assert resolved["status"] == "resolved" and resolved["reply_sent"] is True
    assert services.sms.sent[-1] == ("+12134821234", "I'm Daniel, a local agent.")
    assert client.get("/review", headers=KEY).json() == []


def test_manual_dnc_and_call_outcome(client, services):
    client.post("/prospecting/import-csv", content=CSV, headers=KEY)
    assert client.post("/dnc", json={"phone": "(818) 247-9000"}, headers=KEY).json() == {
        "phone": "+18182479000", "dnc": True}
    sent = client.post("/contact/campaign", json={"mode": "call"}, headers=KEY).json()
    assert sent["call_list"] == 1 and sent["no_phone"] == 1

    services.classifier.will_return("future_follow_up", follow_up_months=12)
    out = client.post("/contact/call-outcome", json={"lead_id": 1, "notes": "Wants to wait a year"}, headers=KEY).json()
    assert out["status"] == "follow_up" and out["follow_up_at"]
    assert services.classifier.calls[-1]["lead"]["channel"] == "call"
    assert client.get("/review?kind=call", headers=KEY).json() == []


def test_server_without_api_key_refuses_requests(settings, services):
    settings.api_key = ""
    client = TestClient(create_app(settings, services))
    assert client.post("/jobs/run-due", headers=KEY).status_code == 503


def test_health_reports_scheduler_state(client):
    assert client.get("/health").json()["scheduler"] == "off"


def test_postgres_urls_from_hosting_providers_are_normalized():
    from leadflow.db import normalize_url

    assert normalize_url("postgres://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"
    assert normalize_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert normalize_url("sqlite://") == "sqlite://"


def test_csv_upload_as_multipart_file_and_excel_guard(client):
    files = {"file": ("propwire.csv", CSV.encode(), "text/csv")}
    result = client.post("/prospecting/import-csv", files=files, headers=KEY).json()
    assert result["created"] == 2

    xlsx = client.post("/prospecting/import-csv", content=b"PK\x03\x04junk", headers=KEY)
    assert xlsx.status_code == 422 and "CSV" in xlsx.json()["detail"]
