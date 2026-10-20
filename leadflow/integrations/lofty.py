"""Lofty CRM client: create/update leads, tags, notes, CMA.

Base URL https://api.lofty.com/v1.0, auth `Authorization: Bearer <key>`.
Paths and field names are kept in PATHS / lead_payload() so they can be checked
against developer.lofty.com for your account in one place. If your plan does not
expose CMA creation through the API, create_cma() fails with a non-retryable
error and the lead goes to the manual review queue ("cma_manual").
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
        "notes": "/leads/{lead_id}/notes",
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
        phones = [p.number for p in lead.phones if p.status not in ("invalid", "wrong_number")]
        emails = [e for e in [lead.email, *(lead.extra_emails or [])] if e]
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
            "propertyType": lead.property_type,
            "callAndText": rules.call_and_text,
        }
        if rules.pond:
            payload["pond"] = rules.pond
        if rules.level:
            payload["level"] = rules.level
        if rules.assigned_agent:
            payload["assignedUser"] = rules.assigned_agent
        return payload

    def upsert_lead(self, lead: Lead, rules: LoftyRules) -> str:
        """Create the lead (or update it if we already have its Lofty id). Returns the Lofty id."""
        if self.dry_run:
            return lead.lofty_lead_id or f"dry-{lead.id}"
        self._ensure("upsert_lead")
        payload = self.lead_payload(lead, rules)
        if lead.lofty_lead_id:
            self._request(
                "update_lead", "PUT", self._url("lead", lead_id=lead.lofty_lead_id),
                json=payload, headers=self._headers(),
            )
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
        self._request(
            "add_note", "POST", self._url("notes", lead_id=lofty_lead_id),
            json={"content": text}, headers=self._headers(),
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
