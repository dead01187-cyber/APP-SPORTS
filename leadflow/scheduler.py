"""Built-in scheduler (SCHEDULER_ENABLED=true), so Zapier only needs two Zaps.

Every minute it checks what is due:
- jobs: follow-ups, CMAs and retries, every `jobs_every_minutes`
- campaign: initial messages once a day at `campaign_time` on `campaign_days`
- reconcile: once a day at `reconcile_time`
- weekly_report: summary sent to ALERT_WEBHOOK_URL once a week

Each run is recorded in scheduler_runs with a unique (task, slot) key, so two
server processes never run the same task twice.
"""

from __future__ import annotations

import calendar
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import reports
from .jobs import run_due_jobs
from .models import SchedulerRun
from .monitoring import log_event, reconcile
from .pipeline.contact import run_campaign
from .rules import Rules
from .timeutil import to_local, utcnow

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger("leadflow.scheduler")

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def due_tasks(rules: Rules, now: datetime) -> list[tuple[str, str]]:
    """Return (task, slot) pairs that should have run by `now` (naive UTC)."""
    cfg = rules.scheduler
    local = to_local(now, rules.contact.quiet_hours.timezone)
    day = WEEKDAYS[local.weekday()]
    today = local.date().isoformat()
    minutes = max(1, cfg.jobs_every_minutes)

    tasks = [("jobs", str(calendar.timegm(now.timetuple()) // (minutes * 60)))]
    if cfg.campaign_enabled and day in cfg.campaign_days and local.time() >= cfg.campaign_time:
        tasks.append(("campaign", today))
    if local.time() >= cfg.reconcile_time:
        tasks.append(("reconcile", today))
    if cfg.weekly_report_day == day and local.time() >= cfg.weekly_report_time:
        tasks.append(("weekly_report", today))
    return tasks


def weekly_report_text(summary: dict[str, Any]) -> str:
    reviews = sum(summary["open_review_items"].values())
    return (
        f"Reporte semanal Leadflow ({summary['period_days']} días): "
        f"{summary['leads_imported']} importados, {summary['contacted']} contactados, "
        f"{summary['replied']} respondieron ({summary['response_rate']:.0%}), "
        f"{summary['cma_sent']} CMAs enviados, {summary['opportunities_hot']} oportunidades Hot, "
        f"{summary['errors_last_24h']} errores en 24 h, {reviews} pendientes de revisión."
    )


def _weekly_report(db: Session, services: Services, now: datetime) -> dict[str, Any]:
    summary = reports.summary(db, services, days=7, now=now)
    services.alerts.send(weekly_report_text(summary))
    return {"sent": True}


TASKS: dict[str, Callable[[Session, "Services", datetime], Any]] = {
    "jobs": lambda db, services, now: run_due_jobs(db, services, now=now),
    "campaign": lambda db, services, now: run_campaign(db, services, mode=services.rules.scheduler.campaign_mode, now=now),
    "reconcile": lambda db, services, now: reconcile(db, now),
    "weekly_report": _weekly_report,
}


def claim(db: Session, task: str, slot: str) -> bool:
    db.add(SchedulerRun(task=task, slot=slot))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def tick(session_factory: sessionmaker, services: Services, now: datetime | None = None) -> dict[str, Any]:
    """Run every task that is due and not yet claimed. Returns what ran."""
    now = now or utcnow()
    results: dict[str, Any] = {}
    for task, slot in due_tasks(services.rules, now):
        with session_factory() as db:
            if not claim(db, task, slot):
                continue
            try:
                results[task] = TASKS[task](db, services, now)
                db.commit()
            except Exception as exc:  # keep the scheduler alive; surface the problem
                log.exception("scheduled task %s failed", task)
                db.rollback()
                log_event(db, "scheduler_error", None, task=task, error=f"{type(exc).__name__}: {exc}")
                db.commit()
                services.alerts.send(f"Leadflow: la tarea programada {task} falló: {exc}")
                results[task] = {"error": str(exc)}
    return results


class SchedulerThread:
    def __init__(self, session_factory: sessionmaker, services: Services, every_seconds: float = 60.0):
        self.session_factory = session_factory
        self.services = services
        self.every_seconds = every_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="leadflow-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = tick(self.session_factory, self.services)
                if ran:
                    log.info("scheduler ran: %s", ", ".join(ran))
            except Exception:  # database down, etc.: try again next minute
                log.exception("scheduler tick failed")
            self._stop.wait(self.every_seconds)
