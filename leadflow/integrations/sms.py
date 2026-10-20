"""SMS provider (Twilio): send texts, look up line type, validate webhook signatures."""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from typing import Any

from .base import HttpService

TWILIO_API = "https://api.twilio.com/2010-04-01"
TWILIO_LOOKUP = "https://lookups.twilio.com/v2/PhoneNumbers"

# Line types we can text. None = unknown (lookup off or unavailable).
TEXTABLE_LINE_TYPES = {"mobile", "nonFixedVoip", "personal", None}

# Twilio error codes
ERR_UNSUBSCRIBED = 21610  # recipient replied STOP at the carrier/Twilio level
ERR_INVALID_NUMBERS = {21211, 21614, 21612}  # invalid / not a mobile / can't route
UNDELIVERABLE_CODES = {"30003", "30005", "30006"}  # unreachable / unknown / landline


class TwilioSms(HttpService):
    service = "sms"

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number)

    def send(self, to: str, body: str, status_callback: str | None = None) -> str:
        if self.dry_run:
            return f"dry-{uuid.uuid4().hex[:16]}"
        self._ensure("send")
        s = self.settings
        data = {"To": to, "From": s.twilio_from_number, "Body": body}
        if status_callback:
            data["StatusCallback"] = status_callback
        result = self._request(
            "send", "POST", f"{TWILIO_API}/Accounts/{s.twilio_account_sid}/Messages.json",
            data=data, auth=(s.twilio_account_sid, s.twilio_auth_token),
        )
        return str(result.get("sid", ""))

    def lookup(self, number: str) -> dict[str, Any]:
        """Return {"valid": bool, "line_type": str | None}."""
        s = self.settings
        if self.dry_run or not self.configured or not s.twilio_lookup_enabled:
            return {"valid": True, "line_type": None}
        result = self._request(
            "lookup", "GET", f"{TWILIO_LOOKUP}/{number}",
            params={"Fields": "line_type_intelligence"},
            auth=(s.twilio_account_sid, s.twilio_auth_token),
        )
        lti = result.get("line_type_intelligence") or {}
        return {"valid": bool(result.get("valid", True)), "line_type": lti.get("type")}

    def validate_signature(self, url: str, params: dict[str, str], signature: str) -> bool:
        token = self.settings.twilio_auth_token
        if not token or not signature:
            return False
        payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
        digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
        expected = base64.b64encode(digest).decode()
        return hmac.compare_digest(expected, signature)
