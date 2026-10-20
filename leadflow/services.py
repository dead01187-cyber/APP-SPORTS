from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings
from .integrations.alerts import Alerter
from .integrations.claude import ClaudeClassifier
from .integrations.enrichment import EnrichmentClient
from .integrations.lofty import LoftyClient
from .integrations.sms import TwilioSms
from .rules import Rules, load_rules


@dataclass
class Services:
    settings: Settings
    rules: Rules
    lofty: Any
    sms: Any
    enrichment: Any
    classifier: Any
    alerts: Any

    def integration_status(self) -> dict[str, str]:
        return {
            "lofty": self.lofty.status(),
            "sms": self.sms.status(),
            "beenverified": self.enrichment.status(),
            "claude": self.classifier.status(),
            "alerts": self.alerts.status(),
        }


def build_services(settings: Settings, rules: Rules | None = None) -> Services:
    return Services(
        settings=settings,
        rules=rules or load_rules(settings.rules_file),
        lofty=LoftyClient(settings),
        sms=TwilioSms(settings),
        enrichment=EnrichmentClient(settings),
        classifier=ClaudeClassifier(settings),
        alerts=Alerter(settings),
    )
