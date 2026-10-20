"""Rules & decision logic, loaded from rules.yaml.

Everything a non-developer may want to tune lives in rules.yaml: filter rules,
Lofty import settings, quiet hours, opt-out keywords, what to do with each
reply category, follow-up timing, CMA settings and the EN/ES message templates.
Missing keys fall back to the defaults below.
"""

from __future__ import annotations

import re
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

CATEGORIES = (
    "interested_selling",
    "interested_buying",
    "not_interested",
    "future_follow_up",
    "wrong_number",
    "question",
    "unclear",
    "opt_out",
)

NextAction = Literal[
    "cma", "notify_agent", "review", "schedule", "schedule_long", "next_phone", "stop", "none"
]


class ProspectingRules(BaseModel):
    required_fields: list[str] = ["owner_name", "property_address"]
    require_phone: bool = True
    max_phones: int = 3
    # Empty list = accept every property type.
    property_types: list[str] = []
    skip_company_owners: bool = False
    # Leave out numbers the list marks Do Not Call, and remember them so they are never texted.
    skip_dnc_phones: bool = True
    # Leave out known TCPA litigators entirely (Propwire "Litigator" column).
    skip_litigators: bool = True
    enrich: bool = True


class LoftyRules(BaseModel):
    source: str = "Propwire"
    tags: list[str] = ["Propwire"]
    pond_name: str | None = None  # for reference only; Lofty needs pond_id
    pond_id: int | None = None
    lead_types: list[int] = []
    ownership_scope: str | None = None  # PERSONAL | TEAM | COMPANY
    call_and_text: bool = True
    assigned_user_id: int | None = None


class QuietHours(BaseModel):
    # Texts are only sent between start and end, local time.
    start: time = time(9, 0)
    end: time = time(20, 0)
    timezone: str = "America/Los_Angeles"


class ContactRules(BaseModel):
    default_mode: Literal["sms", "call"] = "sms"
    batch_limit: int = 100
    require_mobile: bool = True
    quiet_hours: QuietHours = QuietHours()
    opt_out_keywords: list[str] = [
        "STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "OPT OUT",
        "REVOKE", "ALTO", "PARAR", "PARE", "BAJA", "NO MAS",
    ]


class ActionRule(BaseModel):
    status: str = "unchanged"
    tags: list[str] = []
    next: NextAction = "none"


DEFAULT_ACTIONS: dict[str, ActionRule] = {
    "interested_selling": ActionRule(status="hot", tags=["Seller Interested"], next="cma"),
    "interested_buying": ActionRule(status="hot", tags=["Buyer Interested"], next="notify_agent"),
    "question": ActionRule(status="warm", tags=["Question"], next="review"),
    "future_follow_up": ActionRule(status="follow_up", tags=["Future Follow-up"], next="schedule"),
    "not_interested": ActionRule(status="not_interested", tags=["Not Interested"], next="schedule_long"),
    "wrong_number": ActionRule(tags=["Wrong Number"], next="next_phone"),
    "unclear": ActionRule(next="review"),
    "opt_out": ActionRule(status="dnc", tags=["DNC"], next="stop"),
}


class ClassificationRules(BaseModel):
    # Below this confidence a reply goes to the manual review queue.
    min_confidence: float = 0.6
    # Send Claude's suggested reply automatically (off by default).
    auto_reply: bool = False
    auto_reply_categories: list[str] = ["question"]


class FollowUpRules(BaseModel):
    future_default_months: int = 6
    not_interested_months: int = 12  # 0 = never contact again
    max_months: int = 24
    send_hour: int = 10  # local hour for a follow-up on a specific date


class CmaRules(BaseModel):
    auto_run: bool = True
    # Lofty has no CMA endpoint on this account: make the CMA by hand and send the link.
    lofty_api: bool = False
    radius_miles: float = 5
    max_cost: float = 0
    min_comps: int = 3
    eligible_statuses: list[str] = ["hot", "warm"]
    sellers_only: bool = True
    language: str | None = None  # "es" = only Spanish-speaking leads
    follow_up_days: int = 3


class SchedulerRules(BaseModel):
    """Built-in scheduler (SCHEDULER_ENABLED=true). Times are local, in contact.quiet_hours.timezone."""

    jobs_every_minutes: int = 15
    campaign_enabled: bool = True
    campaign_time: time = time(10, 0)
    campaign_days: list[str] = ["mon", "tue", "wed", "thu", "fri"]
    campaign_mode: Literal["sms", "call"] = "sms"
    reconcile_time: time = time(3, 0)
    weekly_report_day: str | None = "mon"
    weekly_report_time: time = time(8, 0)


class TagRules(BaseModel):
    spanish: str = "ES"
    cma_sent: str = "CMA Sent"
    status: dict[str, str] = {
        "hot": "Hot",
        "warm": "Warm",
        "follow_up": "Follow-up",
        "not_interested": "Not Interested",
        "dnc": "DNC",
    }


DEFAULT_TEMPLATES: dict[str, dict[str, str]] = {
    "initial": {
        "en": "Hi {first_name}, this is {agent_name}, a local real estate agent. Would you consider "
        "selling {street}, or would a free market analysis help? Reply STOP to opt out.",
        "es": "Hola {first_name}, soy {agent_name}, agente de bienes raíces local. ¿Consideraría vender "
        "{street} o le serviría un análisis de mercado gratis? Responda STOP para no recibir mensajes.",
    },
    "follow_up": {
        "en": "Hi {first_name}, {agent_name} here following up about {street}. Is selling still on "
        "your radar? Reply STOP to opt out.",
        "es": "Hola {first_name}, le saluda {agent_name} sobre {street}. ¿Sigue pensando en vender? "
        "Responda STOP para no recibir mensajes.",
    },
    "cma": {
        "en": "Hi {first_name}, here is your free market analysis for {street}: {link} "
        "Happy to walk you through it. - {agent_name}, {agent_phone}",
        "es": "Hola {first_name}, aquí está su análisis de mercado gratis para {street}: {link} "
        "Con gusto se lo explico. - {agent_name}, {agent_phone}",
    },
}

FALLBACK_FIRST_NAME = {"en": "there", "es": ""}


class Rules(BaseModel):
    prospecting: ProspectingRules = ProspectingRules()
    lofty: LoftyRules = LoftyRules()
    contact: ContactRules = ContactRules()
    classification: ClassificationRules = ClassificationRules()
    actions: dict[str, ActionRule] = Field(default_factory=lambda: dict(DEFAULT_ACTIONS))
    follow_up: FollowUpRules = FollowUpRules()
    cma: CmaRules = CmaRules()
    scheduler: SchedulerRules = SchedulerRules()
    tags: TagRules = TagRules()
    templates: dict[str, dict[str, str]] = Field(default_factory=lambda: dict(DEFAULT_TEMPLATES))

    def model_post_init(self, __context) -> None:
        # rules.yaml may override only some categories/templates; keep defaults for the rest.
        self.actions = {**DEFAULT_ACTIONS, **self.actions}
        self.templates = {**DEFAULT_TEMPLATES, **self.templates}

    def action_for(self, category: str) -> ActionRule:
        return self.actions.get(category) or self.actions["unclear"]

    def render(self, name: str, lang: str, **ctx: object) -> str:
        variants = self.templates[name]
        template = variants.get(lang) or variants["en"]
        values = {k: ("" if v is None else v) for k, v in ctx.items()}
        if not values.get("first_name"):
            values["first_name"] = FALLBACK_FIRST_NAME.get(lang, "")
        text = template.format_map(_Blank(values))
        text = re.sub(r"\s+([,.!?])", r"\1", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text.rstrip(", ")  # an empty trailing variable leaves "- Name,"


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""


def load_rules(path: str | Path) -> Rules:
    p = Path(path)
    if not p.exists():
        return Rules()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Rules.model_validate(data)
