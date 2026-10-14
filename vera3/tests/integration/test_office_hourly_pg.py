"""load_report на живом Postgres: window-запросы, `has_key` и парное событие блокера.

На SQLite `ev.data.has_key(...)` не исполняется, поэтому весь путь load_report проверяется
только здесь. В CI Postgres поднимается сервисом (deploy.yml).
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://vera:test@localhost:5433/vera_test")

pytestmark = [
    pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION_TESTS"),
                       reason="Set RUN_INTEGRATION_TESTS=1 + provide TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def pg_db(monkeypatch, pg_schema_reset):
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    import vera_shared.db.engine as engine_mod
    from vera_shared.db import models, models_room  # noqa: F401
    from vera_shared.db.engine import close_engine, init_engine

    if engine_mod._engine is not None:
        await close_engine()
    engine = await init_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await pg_schema_reset(conn)
    yield
    await close_engine()


async def held(task_id: str) -> int:
    from vera_shared.room import tasks

    task = await tasks.claim(room="main", task_id=task_id, agent="claude", lease_seconds=600)
    return task["fencing_token"]


def cards_by_id(report) -> dict:
    return {c.row.task_id: c for c in report.cards}


async def test_empty_database_loads_without_error(pg_db):
    from dashboard.office_hourly_service import load_report

    report = await load_report()
    assert report.cards == []
    assert sum(report.counts.values()) == 0


async def test_no_blocked_tasks_loads_and_counts_working(pg_db):
    from dashboard.office_hourly_service import load_report

    await held("P1")
    report = await load_report()
    assert cards_by_id(report)["P1"].status == "В работе"
    assert report.counts["В работе"] == 1


async def test_blocked_flip_with_paired_note_is_confirmed_with_reason(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.room import tasks

    token = await held("P2")
    await tasks.update(room="main", task_id="P2", agent="claude", fencing_token=token,
                       status="blocked", note="reason")
    card = cards_by_id(await load_report())["P2"]
    assert card.status == "Блокер"
    assert card.info.blocker_confidence == "подтверждён"
    assert card.info.wait_reason == "reason"


async def test_blocked_without_note_then_unrelated_progress_is_unconfirmed(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.room import tasks

    token = await held("P3")
    await tasks.update(room="main", task_id="P3", agent="claude", fencing_token=token,
                       status="blocked")
    await tasks.update(room="main", task_id="P3", agent="claude", fencing_token=token,
                       note="unrelated progress")
    card = cards_by_id(await load_report())["P3"]
    assert card.status == "Блокер"
    assert card.info.blocker_confidence == "не подтверждён"
    assert card.info.wait_reason is None


async def test_done_and_cancelled_are_absent_from_cards_and_counters(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_room import RoomTaskRow
    from vera_shared.room import tasks

    token = await held("P4")
    await tasks.release(room="main", task_id="P4", agent="claude", fencing_token=token,
                        status="done")
    await tasks.open_task(room="main", task_id="P5", agent="claude", title="t", paths=None)
    async with get_session() as s:
        row = await s.get(RoomTaskRow, ("main", "P5"))
        row.status = "cancelled"
        await s.commit()
    await held("P6")
    report = await load_report()
    assert set(cards_by_id(report)) == {"P6"}
    assert sum(report.counts.values()) == 1


async def test_card_details_artifact_executor_and_refs_pg(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.room import task_progress, tasks

    await tasks.open_task(room="main", task_id="C1", agent="claude", title="t", paths=None,
                          responsible="dot", next_action="ship it",
                          refs=[{"kind": "url", "ref": "https://example.org/a"}])
    card = cards_by_id(await load_report())["C1"]
    assert card.details.executor.label == "ответственный"
    assert card.details.artifact is None
    assert card.details.next_action == "ship it"
    token = await held("C1")
    await task_progress.progress(room="main", task_id="C1", agent="claude",
                                 fencing_token=token, result="shipped slice",
                                 next_checkpoint_seconds=300)
    card = cards_by_id(await load_report())["C1"]
    assert card.details.executor.name == "claude"
    assert card.details.executor.label == "держатель аренды"
    assert card.details.artifact.text == "shipped slice"
    assert card.details.artifact.refs[0].url == "https://example.org/a"
    assert card.details.delta.new_artifacts == 1


async def test_card_details_dependency_status_pg(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.room import tasks

    token = await held("D1")
    await tasks.release(room="main", task_id="D1", agent="claude", fencing_token=token,
                        status="done")
    await tasks.open_task(room="main", task_id="D2", agent="claude", title="t", paths=None,
                          depends_on=["D1", "missing"])
    card = cards_by_id(await load_report())["D2"]
    got = {d.task_id: d.status for d in card.details.dependencies}
    assert got == {"D1": "Готово", "missing": "не найдена"}


async def test_card_details_hourly_delta_pg(pg_db):
    from datetime import timedelta

    from dashboard.office_hourly_service import load_report
    from vera_shared.room import tasks

    token = await held("H1")
    await held("H2")
    await tasks.update(room="main", task_id="H1", agent="claude", fencing_token=token,
                       status="blocked", note="stuck")
    cards = cards_by_id(await load_report())
    assert "блокер" in [label for _, label in cards["H1"].details.delta.transitions]
    assert not cards["H1"].details.delta.unchanged
    assert cards["H2"].details.delta.transitions[0][1] == "взята"
    later = await load_report(now=cards["H1"].row.updated_at + timedelta(hours=2))
    assert cards_by_id(later)["H1"].details.delta.unchanged


async def test_waiting_with_expired_lease_is_not_blocker_pg(pg_db):
    from datetime import datetime, timedelta

    from dashboard.office_hourly_service import load_report
    from vera_shared.room import task_wait

    token = await held("W1")
    task = await task_wait.wait(room="main", task_id="W1", agent="claude",
                                fencing_token=token, until_seconds=600, reason="ждём решение")
    later = await load_report(now=datetime.fromisoformat(task["updated_at"]) + timedelta(hours=3))
    card = cards_by_id(later)["W1"]
    assert card.status == "Новая / в очереди"
    assert card.info.waiting is True
    assert card.info.wait_reason == "ждём решение"
    assert card.info.decision_owner is None
    assert card.info.wait_freshness == "stale"
    assert card.info.blocker_confidence is None


async def test_waiting_decision_owner_explicit_row_field_pg(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.room import task_progress, task_wait

    token = await held("W2")
    await task_wait.wait(room="main", task_id="W2", agent="claude", fencing_token=token,
                         until_seconds=600, reason="нужен ответ")
    assert cards_by_id(await load_report())["W2"].info.decision_owner is None
    task = await task_wait.wait(room="main", task_id="W2", agent="claude", fencing_token=token,
                                until_seconds=600, reason="нужен ответ",
                                decision_owner=" owner ")
    assert task["waiting_decision_owner"] == "owner"
    card = cards_by_id(await load_report())["W2"]
    assert card.status == "В работе"
    assert card.info.decision_owner == "owner"
    await task_progress.progress(room="main", task_id="W2", agent="claude",
                                 fencing_token=token, result="решено",
                                 next_checkpoint_seconds=300)
    cleared = cards_by_id(await load_report())["W2"].info
    assert cleared.waiting is False
    assert cleared.decision_owner is None


async def test_decision_owner_rejected_when_too_long_pg(pg_db):
    import pytest
    from vera_shared.room import task_wait

    token = await held("W4")
    with pytest.raises(ValueError, match="decision_owner"):
        await task_wait.wait(room="main", task_id="W4", agent="claude", fencing_token=token,
                             until_seconds=600, reason="r", decision_owner="x" * 65)


async def test_wait_then_progress_clears_waiting_pg(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.room import task_progress, task_wait

    token = await held("X1")
    await task_wait.wait(room="main", task_id="X1", agent="claude", fencing_token=token,
                         until_seconds=600, reason="need review")
    assert cards_by_id(await load_report())["X1"].info.waiting is True
    await task_progress.progress(room="main", task_id="X1", agent="claude",
                                 fencing_token=token, result="resolved",
                                 next_checkpoint_seconds=300)
    info = cards_by_id(await load_report())["X1"].info
    assert info.waiting is False
    assert info.wait_reason is None
    assert info.wait_freshness is None


async def test_wait_then_update_clears_waiting_pg(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.room import task_wait, tasks

    token = await held("X2")
    await task_wait.wait(room="main", task_id="X2", agent="claude", fencing_token=token,
                         until_seconds=600, reason="need input")
    await tasks.update(room="main", task_id="X2", agent="claude", fencing_token=token,
                       note="got it")
    info = cards_by_id(await load_report())["X2"].info
    assert info.waiting is False
    assert info.wait_reason is None


async def test_active_wait_shows_waiting_with_reason_pg(pg_db):
    from dashboard.office_hourly_service import load_report
    from vera_shared.room import task_wait

    token = await held("X3")
    await task_wait.wait(room="main", task_id="X3", agent="claude", fencing_token=token,
                         until_seconds=600, reason="ждём CI")
    card = cards_by_id(await load_report())["X3"]
    assert card.status == "В работе"
    assert card.info.waiting is True
    assert card.info.wait_reason == "ждём CI"
    assert card.info.wait_freshness == "fresh"


async def test_scope_filter_hides_and_counts_without_touching_data_pg(pg_db):
    from dashboard.office_hourly_service import load_report
    from sqlalchemy import func, select
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_room import RoomTaskRow
    from vera_shared.room import tasks

    for task_id, project in (("SIN-77", None), ("S1", "Sintegrum"), ("O1", "Orchestra"),
                             ("O2", "Orchestra"), ("V1", "Vera")):
        await tasks.open_task(room="main", task_id=task_id, agent="claude", title="t",
                              paths=None, project=project)
    await tasks.claim(room="main", task_id="O2", agent="claude", lease_seconds=600)
    report = await load_report()
    assert set(cards_by_id(report)) == {"O2", "V1"}
    assert (report.hidden_excluded, report.hidden_inactive) == (2, 1)
    assert sum(report.counts.values()) == 2
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(RoomTaskRow))
    assert total == 5


async def test_visible_card_dependency_on_hidden_task_is_masked_pg(pg_db):
    from dashboard.office_hourly_service import load_report
    from dashboard.office_hourly_view import report_body
    from vera_shared.room import tasks

    await tasks.open_task(room="main", task_id="SIN-123", agent="claude", title="t", paths=None)
    await tasks.open_task(room="main", task_id="V2", agent="claude", title="t", paths=None,
                          project="Vera", depends_on=["SIN-123"])
    report = await load_report()
    assert set(cards_by_id(report)) == {"V2"}
    deps = cards_by_id(report)["V2"].details.dependencies
    assert [(d.task_id, d.status) for d in deps] == [("", "скрыто фильтром Office")]
    assert "SIN-123" not in report_body(report)
