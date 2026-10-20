from datetime import datetime, timedelta

from sqlalchemy import select

from leadflow.db import make_sessionmaker
from leadflow.models import Event, Message
from leadflow.scheduler import TASKS, due_tasks, tick

from .conftest import row

# Times are UTC; Los Angeles is UTC-7 in October.
MON_0905 = datetime(2026, 10, 19, 16, 5)   # Monday 09:05 local
MON_1005 = datetime(2026, 10, 19, 17, 5)   # Monday 10:05 local
SAT_1100 = datetime(2026, 10, 24, 18, 0)   # Saturday 11:00 local


def factory(db):
    return make_sessionmaker(db.get_bind())


def test_due_tasks_follow_the_configured_times(services):
    assert [t for t, _ in due_tasks(services.rules, MON_0905)] == ["jobs", "reconcile", "weekly_report"]
    assert [t for t, _ in due_tasks(services.rules, MON_1005)] == ["jobs", "campaign", "reconcile", "weekly_report"]
    assert [t for t, _ in due_tasks(services.rules, SAT_1100)] == ["jobs", "reconcile"]  # no weekend campaign


def test_each_task_runs_once_per_slot(db, services):
    from leadflow.pipeline.prospecting import ingest_rows

    ingest_rows(db, services, [row()])
    db.commit()
    sessions = factory(db)

    first = tick(sessions, services, MON_1005)
    assert set(first) == {"jobs", "campaign", "reconcile", "weekly_report"}
    assert first["campaign"]["sent"] == 1
    assert services.alerts.sent[-1].startswith("Reporte semanal Leadflow (7 días)")

    again = tick(sessions, services, MON_1005 + timedelta(minutes=5))
    assert again == {}  # same 15-minute slot, campaign/reconcile/report already done today

    later = tick(sessions, services, MON_1005 + timedelta(minutes=15))
    assert set(later) == {"jobs"}
    assert len(db.scalars(select(Message)).all()) == 1  # nobody texted twice


def test_a_failing_task_alerts_and_does_not_stop_the_others(db, services, monkeypatch):
    def boom(*_args):
        raise RuntimeError("lofty down")

    monkeypatch.setitem(TASKS, "reconcile", boom)
    results = tick(factory(db), services, SAT_1100)
    assert results["reconcile"] == {"error": "lofty down"}
    assert "jobs" in results
    assert any("reconcile falló" in a for a in services.alerts.sent)
    assert db.scalars(select(Event).where(Event.type == "scheduler_error")).first() is not None
