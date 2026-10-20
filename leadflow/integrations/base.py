from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger("leadflow.integrations")


class IntegrationError(Exception):
    """An external API call failed. `retryable` tells the job runner whether to try again."""

    def __init__(
        self,
        service: str,
        operation: str,
        message: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
        code: int | str | None = None,
    ):
        super().__init__(f"{service}.{operation}: {message}")
        self.service = service
        self.operation = operation
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.code = code  # provider-specific error code (e.g. Twilio 21610)


class HttpService:
    service = "http"

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.settings = settings
        self.http = http or httpx.Client(timeout=20.0)

    @property
    def configured(self) -> bool:
        return False

    @property
    def dry_run(self) -> bool:
        return self.settings.dry_run

    @property
    def available(self) -> bool:
        return self.configured or self.dry_run

    def status(self) -> str:
        if self.dry_run:
            return "dry_run"
        return "configured" if self.configured else "not_configured"

    def _ensure(self, operation: str) -> None:
        if not self.configured:
            raise IntegrationError(self.service, operation, "not configured", retryable=False)

    def _request(self, operation: str, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise IntegrationError(self.service, operation, f"network error: {exc}") from exc
        if response.status_code >= 400:
            code = None
            try:
                body = response.json()
                if isinstance(body, dict):
                    code = body.get("code") or body.get("errorCode")
            except ValueError:
                pass
            retryable = response.status_code in (408, 409, 429) or response.status_code >= 500
            raise IntegrationError(
                self.service,
                operation,
                f"HTTP {response.status_code}: {response.text[:300]}",
                retryable=retryable,
                status_code=response.status_code,
                code=code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"text": response.text}
