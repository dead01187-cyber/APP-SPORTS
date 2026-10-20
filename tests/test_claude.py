import json
from datetime import date
from types import SimpleNamespace

import pytest

from leadflow.integrations.claude import ClassificationError, ClaudeClassifier


class FakeAnthropic:
    def __init__(self, response):
        self.kwargs = None
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                return response

        self.beta = SimpleNamespace(messages=_Messages())


def response(payload, stop_reason="end_turn"):
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(payload))]
    return SimpleNamespace(stop_reason=stop_reason, content=content)


GOOD = {
    "category": "future_follow_up", "confidence": 0.9, "language": "es", "follow_up_months": 6,
    "follow_up_date": "", "summary": "Owner may sell next year.", "suggested_reply": "¡Gracias! Le escribo pronto.",
}


def test_classify_sends_structured_request_and_parses(settings):
    client = FakeAnthropic(response(GOOD))
    result = ClaudeClassifier(settings, client).classify(
        "Tal vez el próximo año", history=[{"direction": "agent", "body": "Hola"}],
        lead={"owner_name": "Maria Lopez"}, today=date(2026, 10, 20),
    )
    assert result.category == "future_follow_up" and result.language == "es" and result.follow_up_months == 6
    kw = client.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    prompt = kw["messages"][0]["content"]
    assert "Today's date: 2026-10-20" in prompt
    assert "<reply>\nTal vez el próximo año\n</reply>" in prompt
    assert "[agent] Hola" in prompt


def test_unknown_category_and_out_of_range_confidence_are_sanitized(settings):
    client = FakeAnthropic(response({**GOOD, "category": "maybe", "confidence": 3}))
    result = ClaudeClassifier(settings, client).classify("??")
    assert result.category == "unclear" and result.confidence == 1.0


def test_refusal_raises(settings):
    client = FakeAnthropic(response(GOOD, stop_reason="refusal"))
    with pytest.raises(ClassificationError):
        ClaudeClassifier(settings, client).classify("text")


def test_available_reflects_api_key(settings, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ClaudeClassifier(settings).available is False
    settings.anthropic_api_key = "sk-test"
    assert ClaudeClassifier(settings).available is True
