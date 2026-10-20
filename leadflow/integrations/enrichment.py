"""Contact enrichment (BeenVerified or a similar people-search API).

BeenVerified does not publish one standard public API, so this client POSTs
{name, address, city, state, zip} as JSON to BEENVERIFIED_API_URL with a Bearer
key and accepts common response shapes. Adjust parse_response() to your account,
or skip this client and push enrichment results from Zapier instead.
"""

from __future__ import annotations

from typing import Any

from .base import HttpService


class EnrichmentClient(HttpService):
    service = "beenverified"

    @property
    def configured(self) -> bool:
        return bool(self.settings.beenverified_api_key and self.settings.beenverified_api_url)

    def lookup(self, lead: dict[str, Any]) -> dict[str, list]:
        if self.dry_run:
            return {"phones": [], "emails": [], "relatives": []}
        self._ensure("lookup")
        data = self._request(
            "lookup", "POST", self.settings.beenverified_api_url,
            json={
                "name": lead.get("owner_name"),
                "first_name": lead.get("first_name"),
                "last_name": lead.get("last_name"),
                "address": lead.get("property_address"),
                "city": lead.get("city"),
                "state": lead.get("state"),
                "zip": lead.get("zip_code"),
            },
            headers={"Authorization": f"Bearer {self.settings.beenverified_api_key}"},
        )
        return parse_response(data)


def parse_response(data: Any) -> dict[str, list]:
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        return {"phones": [], "emails": [], "relatives": []}

    def values(key: str, *fields: str) -> list[str]:
        out = []
        for item in data.get(key) or []:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.extend(str(item[f]) for f in fields if item.get(f))
        return out

    return {
        "phones": values("phones", "number", "phone", "phone_number"),
        "emails": values("emails", "address", "email"),
        "relatives": values("relatives", "name", "full_name"),
    }
