"""Lofty CRM client: create/update leads, tags, notes, CMA.

Base URL https://api.lofty.com/v1.0, auth `Authorization: Bearer <key>`.

Tested against the live Lofty account (Oct 2026, with a test lead that was then deleted):
- POST /leads -> {"leadId": ...}. Saved: firstName, lastName, emails[], phones[]
  (10 digits), tags (list of names), streetAddress/city/state/zipCode, cannotText,
  cannotCall, leadTypes (1 = Seller).
- `source` only keeps names from Lofty's own source list; anything else becomes
  "Other". The "Propwire" tag is the reliable marker.
- Leads are assigned to the owner of the API key as PERSONAL unless pondId or
  assignedUserId is sent.
- PUT /leads/{id} updates tags and the cannotText/cannotCall flags. It answers
  HTTP 200 with {"status": {"code": 0}}; a non-zero code is an error.
- POST /notes {"leadId", "content"} -> {"noteId": ...}.
- There is no CMA endpoint (/cma is 404). With cma.lofty_api: false (default)
  CMAs are made by hand and the link is sent through /cma/{id}/form.
"""

from __future__ import annotations

from typing import Any

from ..models import Lead
from ..rules import CmaRules, LoftyRules
from .base import HttpService, IntegrationError


class LoftyClient(HttpService):
    service = "lofty"

    PATHS = {
        "create_lead": "/leads",
        "lead": "/leads/{lead_id}",
        "notes": "/notes",
        "cma": "/cma",
    }

    @property
    def configured(self) -> bool:
        return bool(self.settings.lofty_api_key)

    def _url(self, name: str, **params: Any) -> str:
        return self.settings.lofty_base_url.rstrip("/") + self.PATHS[name].format(**params)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.lofty_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def lead_payload(lead: Lead, rules: LoftyRules) -> dict[str, Any]:
        phones = [_lofty_phone(p.number) for p in lead.phones if p.status not in ("invalid", "wrong_number")]
        emails = [e for e in [lead.email, *(lead.extra_emails or [])] if e]
        blocked = lead.status == "dnc" or not rules.call_and_text
        payload: dict[str, Any] = {
            "firstName": lead.first_name or lead.owner_name,
            "lastName": lead.last_name or "",
            "emails": emails,
            "phones": phones,
            "source": rules.source,
            "tags": list(dict.fromkeys([*rules.tags, *(lead.tags or [])])),
            "streetAddress": lead.property_address,
            "city": lead.city,
            "state": lead.state,
            "zipCode": lead.zip_code,
            # "Call & Text: Yes" for new leads; switched off when the person opts out.
            "cannotText": blocked,
            "cannotCall": blocked,
        }
        if rules.pond_id:
            payload["pondId"] = rules.pond_id
        if rules.lead_types:
            payload["leadTypes"] = rules.lead_types
        if rules.ownership_scope:
            payload["ownershipScope"] = rules.ownership_scope
        if rules.assigned_user_id:
            payload["assignedUserId"] = rules.assigned_user_id
        return payload

    def upsert_lead(self, lead: Lead, rules: LoftyRules) -> str:
        """Create the lead (or update it if we already have its Lofty id). Returns the Lofty id."""
        if self.dry_run:
            return lead.lofty_lead_id or f"dry-{lead.id}"
        self._ensure("upsert_lead")
        payload = self.lead_payload(lead, rules)
        if lead.lofty_lead_id:
            data = self._request(
                "update_lead", "PUT", self._url("lead", lead_id=lead.lofty_lead_id),
                json=payload, headers=self._headers(),
            )
            status = data.get("status") if isinstance(data, dict) else None
            if isinstance(status, dict) and status.get("code") not in (0, None):
                raise IntegrationError(self.service, "update_lead", f"Lofty error: {status}", retryable=False)
            return lead.lofty_lead_id
        data = self._request("create_lead", "POST", self._url("create_lead"), json=payload, headers=self._headers())
        lofty_id = _find_id(data, "leadId", "id")
        if not lofty_id:
            raise IntegrationError(self.service, "create_lead", f"no lead id in response: {data}", retryable=False)
        return str(lofty_id)

    def add_note(self, lofty_lead_id: str, text: str) -> None:
        if self.dry_run:
            return
        self._ensure("add_note")
        lead_id: int | str = int(lofty_lead_id) if str(lofty_lead_id).isdigit() else lofty_lead_id
        self._request(
            "add_note", "POST", self._url("notes"),
            json={"leadId": lead_id, "content": text}, headers=self._headers(),
        )

    def create_cma(self, lead: Lead, rules: CmaRules) -> dict[str, Any]:
        """Ask Lofty for a CMA. Returns {id, url, comps, cost}."""
        if self.dry_run:
            return {"id": f"dry-cma-{lead.id}", "url": f"https://example.com/cma/{lead.id}", "comps": 5, "cost": 0}
        self._ensure("create_cma")
        payload = {
            "leadId": lead.lofty_lead_id,
            "address": lead.property_address,
            "city": lead.city,
            "state": lead.state,
            "zipCode": lead.zip_code,
            "propertyType": lead.property_type,
            "radiusMiles": rules.radius_miles,
            "autoComparables": True,
            "maxCost": rules.max_cost,
        }
        try:
            data = self._request("create_cma", "POST", self._url("cma"), json=payload, headers=self._headers())
        except IntegrationError as exc:
            if exc.status_code in (404, 405, 501):
                exc.retryable = False
                exc.message = "CMA endpoint not available on this Lofty account"
            raise
        body = data.get("data", data) if isinstance(data, dict) else {}
        comps = body.get("comparables") or body.get("comps") or []
        return {
            "id": _find_id(body, "cmaId", "id"),
            "url": body.get("url") or body.get("link") or body.get("shareUrl"),
            "comps": len(comps) if isinstance(comps, list) else int(comps or 0),
            "cost": float(body.get("cost") or 0),
        }


def _lofty_phone(number: str) -> str:
    """Lofty stores US numbers as 10 digits (+12134821234 -> 2134821234)."""
    return number[2:] if number.startswith("+1") and len(number) == 12 else number


def _find_id(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if data.get(key):
            return data[key]
    inner = data.get("data")
    if isinstance(inner, dict):
        return _find_id(inner, *keys)
    return None
