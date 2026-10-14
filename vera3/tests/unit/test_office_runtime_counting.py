"""Rendered-body regression tests for the shared runtime counting rule."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dashboard.office_hourly_service import build_report
from dashboard.office_hourly_view import report_body
from dashboard.office_runtime_service import PREFIX, parse_runtime_snapshot
from vera_shared.room.attention import Attention

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
UUID = "01a12abb-b3b4-74dc-aeeb-3432e9fd509f"
STALE = (NOW - timedelta(minutes=90)).isoformat()
FRESH_AT = (NOW - timedelta(minutes=5)).isoformat()


def body_for(entries: list[dict], sources: dict[str, dict], *,
             default_observed: bool = True) -> str:
    if default_observed:
        sources = {k: {"observed_at": FRESH_AT, **v} for k, v in sources.items()}
    raw = {"checked_at": NOW.isoformat(), "entries": entries, "sources": sources}
    msg = SimpleNamespace(room="main", from_agent="dot", to_agent="office-dashboard",
                          task_id=None, status="info", body=PREFIX + json.dumps(raw),
                          created_at=NOW, message_id="m")
    snapshot = parse_runtime_snapshot(msg, NOW)
    assert snapshot is not None
    task = SimpleNamespace(room="main", task_id="t", title="t", project="Proj", status="open",
                           priority=2, lease_holder=None, lease_until=None, responsible=None,
                           next_action=None, note=None, last_progress_at=None,
                           waiting_reason=None)
    report = build_report([task], {("main", "t"): Attention("unassigned", "", None, None, None)},
                          [], [], NOW)
    return report_body(replace(report, runtime=snapshot))


def entry(state: str, source: str = "claude", sid: str = "a", project: str = "Proj") -> dict:
    return {"project": project, "state": state, "detail": "d", "source": source,
            "source_id": sid}


def test_completed_only_partial_source_is_not_zero_confirmed() -> None:
    html = body_for([entry("completed")], {"claude": {"coverage": "partial"}})
    assert "проверенные runtime в работе: не подтверждено" in html
    assert "0 подтверждено" not in html
    assert "Активных потоков нет" not in html


def test_stale_source_active_entry_is_not_confirmed_in_fresh_snapshot() -> None:
    html = body_for([entry("active", "claude", "a"),
                     {"project": "Proj", "state": "active", "detail": "d", "thread_id": UUID}],
                    {"claude": {"coverage": "complete", "observed_at": STALE},
                     "codex": {"coverage": "partial", "observed_at": FRESH_AT}})
    assert "Подтверждённые активные потоки: 1; не подтверждено: 1" in html
    assert "проверенные runtime в работе: 1 подтверждено; не подтверждено: 1" in html
    assert "claude: наблюдение устарело" in html


@pytest.mark.parametrize("coverage", ["unavailable"])
def test_unavailable_source_never_confirms_active(coverage: str) -> None:
    html = body_for([entry("active")], {"claude": {"coverage": coverage}})
    assert "Подтверждённые активные потоки" not in html
    assert "Активные потоки не подтверждены" in html
    assert "проверенные runtime в работе: не подтверждено" in html


def test_unknown_source_never_confirms_active() -> None:
    html = body_for([entry("active", "unknown", "")], {})
    assert "Подтверждённые активные потоки" not in html
    assert "проверенные runtime в работе: не подтверждено" in html


def test_complete_fresh_source_with_unknown_state_is_not_zero() -> None:
    html = body_for([entry("unknown")], {"claude": {"coverage": "complete"}})
    assert "Активных потоков нет" not in html
    assert "0 подтверждено" not in html
    assert "проверенные runtime в работе: не подтверждено" in html
    assert "0 активных" not in html


def test_complete_fresh_source_completed_only_is_proven_zero() -> None:
    html = body_for([entry("completed")], {"claude": {"coverage": "complete"}})
    assert "Активных потоков нет (покрытие полное)" in html
    assert "проверенные runtime в работе: 0 подтверждено" in html
    assert "claude: 0 активных (покрытие полное)" in html


def test_partial_fresh_active_counts_as_floor() -> None:
    html = body_for([entry("active")], {"claude": {"coverage": "partial"}})
    assert "Подтверждённые активные потоки: 1" in html
    assert "claude: не менее 1 активных (покрытие partial)" in html


def test_project_zero_not_claimed_when_another_declared_source_is_unavailable() -> None:
    html = body_for(
        [{"project": "Proj", "state": "completed", "detail": "d", "thread_id": UUID}],
        {"codex": {"coverage": "complete"}, "claude": {"coverage": "unavailable"}})
    assert "0 подтверждено" not in html
    assert "проверенные runtime в работе: не подтверждено" in html
    assert "Активных потоков нет" not in html


def test_hostile_source_id_and_detail_are_escaped() -> None:
    hostile = {"project": "Proj", "state": "active", "source": "claude",
               "source_id": "<script>alert(1)</script>", "detail": "<img src=x onerror=1>"}
    html = body_for([hostile], {"claude": {"coverage": "partial"}})
    assert "<script>alert(1)" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)" in html
    assert "&lt;img src=x" in html


def test_missing_observed_at_is_unknown_freshness_not_checked_at() -> None:
    html = body_for([entry("active")], {"claude": {"coverage": "complete"}},
                    default_observed=False)
    assert "Подтверждённые активные потоки" not in html
    assert "0 подтверждено" not in html
    assert "Активных потоков нет" not in html
    assert "claude: не указано время наблюдения, текущий счёт неизвестен" in html
    assert "проверенные runtime в работе: не подтверждено" in html
