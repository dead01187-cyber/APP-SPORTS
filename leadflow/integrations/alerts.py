"""Alert notifications. Posts {"text": ...} to a Slack incoming webhook or a
Zapier catch hook (a Zap can forward it by email). Never raises."""

from __future__ import annotations

from .base import HttpService, IntegrationError, log


class Alerter(HttpService):
    service = "alerts"

    @property
    def configured(self) -> bool:
        return bool(self.settings.alert_webhook_url)

    def send(self, text: str) -> None:
        log.warning("ALERT: %s", text)
        if self.dry_run or not self.configured:
            return
        try:
            self._request("send", "POST", self.settings.alert_webhook_url, json={"text": text})
        except IntegrationError as exc:
            log.error("alert delivery failed: %s", exc)
