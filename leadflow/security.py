"""Signed links, so a person can act from an alert (email/SMS) without the API key."""

from __future__ import annotations

import hashlib
import hmac

from .config import Settings


def cma_token(settings: Settings, lead_id: int) -> str:
    return hmac.new(settings.api_key.encode(), f"cma:{lead_id}".encode(), hashlib.sha256).hexdigest()[:32]


def check_cma_token(settings: Settings, lead_id: int, token: str | None) -> bool:
    return bool(settings.api_key and token) and hmac.compare_digest(cma_token(settings, lead_id), token or "")


def cma_form_url(settings: Settings, lead_id: int) -> str | None:
    if not (settings.base_url and settings.api_key):
        return None
    return f"{settings.base_url}/cma/{lead_id}/form?t={cma_token(settings, lead_id)}"
