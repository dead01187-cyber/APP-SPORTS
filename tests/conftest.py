"""Shared fixtures. Every external service is replaced by an in-memory fake."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from leadflow.config import Settings
from leadflow.db import Base, init_db, make_engine, make_sessionmaker
from leadflow.integrations.claude import Classification
from leadflow.integrations.lofty import LoftyClient
from leadflow.integrations.sms import TwilioSms
from leadflow.pipeline.prospecting import ingest_rows
from leadflow.rules import load_rules
from leadflow.services import Services

ROOT = Path(__file__).resolve().parent.parent

# 2026-10-20 18:00 UTC = 11:00 in Los Angeles (inside sending hours)
DAYTIME = datetime(2026, 10, 20, 18, 0)
# 2026-10-21 06:00 UTC = 23:00 in Los Angeles (quiet hours)
NIGHT = datetime(2026, 10, 21, 6, 0)


class FakeLofty:
    def __init__(self) -> None:
        self.payloads: dict[str, dict] = {}
        self.notes: list[tuple[str, str]] = []
        self.fail_upsert: Exception | None = None
        self.cma_error: Exception | None = None
        self.cma_result = {"id": "cma-1", "url": "https://lofty.example/cma/1", "comps": 6, "cost": 0}
        self.cma_requests = 0
        self._next_id = 9_000_000_000

    def status(self) -> str:
        return "fake"

    def upsert_lead(self, lead, rules) -> str:
        if self.fail_upsert:
            raise self.fail_upsert
        if not lead.lofty_lead_id:
            self._next_id += 1
        lofty_id = lead.lofty_lead_id or str(self._next_id)
        self.payloads[lofty_id] = LoftyClient.lead_payload(lead, rules)
        return lofty_id

    def add_note(self, lofty_id: str, text: str) -> None:
        self.notes.append((lofty_id, text))

    def create_cma(self, lead, rules) -> dict:
        self.cma_requests += 1
        if self.cma_error:
            raise self.cma_error
        return dict(self.cma_result)


class FakeSms:
    def __init__(self, settings: Settings) -> None:
        self.sent: list[tuple[str, str]] = []
        self.line_types: dict[str, str] = {}
        self.fail: Exception | None = None
        self._real = TwilioSms(settings)

    available = True

    def status(self) -> str:
        return "fake"

    def send(self, to: str, body: str, status_callback: str | None = None) -> str:
        if self.fail:
            raise self.fail
        self.sent.append((to, body))
        return f"SM{len(self.sent):04d}"

    def lookup(self, number: str) -> dict[str, Any]:
        return {"valid": True, "line_type": self.line_types.get(number, "mobile")}

    def validate_signature(self, url: str, params: dict, signature: str) -> bool:
        return self._real.validate_signature(url, params, signature)


class FakeEnrichment:
    available = True

    def __init__(self) -> None:
        self.results: dict[str, dict] = {}

    def status(self) -> str:
        return "fake"

    def lookup(self, lead: dict) -> dict:
        return self.results.get(lead.get("owner_name") or "", {"phones": [], "emails": [], "relatives": []})


class FakeClassifier:
    available = True

    def __init__(self) -> None:
        self.queue: list[Classification | Exception] = []
        self.calls: list[dict] = []

    def status(self) -> str:
        return "fake"

    def will_return(self, category: str, confidence: float = 0.95, **kw: Any) -> None:
        self.queue.append(Classification(category=category, confidence=confidence, summary=f"{category}", **kw))

    def classify(self, message, history=None, lead=None, today=None) -> Classification:
        self.calls.append({"message": message, "history": history, "lead": lead, "today": today})
        result = self.queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeAlerts:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def status(self) -> str:
        return "fake"

    def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def database_url() -> str:
    """In-memory SQLite by default. Set TEST_DATABASE_URL to run the suite on Postgres."""
    url = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    if not url.startswith("sqlite"):
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        init_db(engine)
        engine.dispose()
    return url


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        api_key="test-key",
        twilio_auth_token="twilio-token",
        agent_name="Daniel",
        rules_file=str(ROOT / "rules.yaml"),
    )


@pytest.fixture
def services(settings: Settings) -> Services:
    services = Services(
        settings=settings,
        rules=load_rules(ROOT / "rules.yaml"),
        lofty=FakeLofty(),
        sms=FakeSms(settings),
        enrichment=FakeEnrichment(),
        classifier=FakeClassifier(),
        alerts=FakeAlerts(),
    )
    # Most tests simulate an account with a CMA API; test_jobs_cma covers the manual default.
    services.rules.cma.lofty_api = True
    return services


@pytest.fixture
def db(database_url: str):
    engine = make_engine(database_url)
    init_db(engine)
    session = make_sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def row(**overrides: Any) -> dict[str, Any]:
    base = {
        "Owner Name": "JOHN SMITH",
        "Property Address": "123 MAIN STREET",
        "City": "LOS ANGELES",
        "State": "ca",
        "Zip": "90012-1234",
        "Property Type": "Single Family Residential",
        "Phone 1": "(213) 482-1234",
        "Phone 2": "",
        "Email": "John@Example.com",
    }
    base.update(overrides)
    return base


@pytest.fixture
def make_lead(db, services):
    def _make(**overrides: Any):
        report = ingest_rows(db, services, [row(**overrides)])
        db.commit()
        assert report.created == 1, report.as_dict()
        from leadflow.models import Lead

        return db.get(Lead, report.lead_ids[0])

    return _make
