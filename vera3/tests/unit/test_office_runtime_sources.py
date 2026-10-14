"""OFFICE_RUNTIME_V1 backward-compatible source extension and coverage-aware counters."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from dashboard.office_runtime_service import PREFIX, parse_runtime_snapshot
from dashboard.office_runtime_summary import source_summaries

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
UUID = "01a12abb-b3b4-74dc-aeeb-3432e9fd509f"


def message(entries: list[dict], sources: dict | None = None) -> SimpleNamespace:
    raw: dict = {"checked_at": NOW.isoformat(), "entries": entries}
    if sources is not None:
        raw["sources"] = sources
    return SimpleNamespace(room="main", from_agent="dot", to_agent="office-dashboard",
                           task_id=None, status="info", body=PREFIX + json.dumps(raw),
                           created_at=NOW, message_id="m1")


def entry(**kw) -> dict:
    return {"project": "P", "state": "active", "detail": "d", **kw}


def test_existing_v1_message_still_parses_as_codex_with_unknown_coverage() -> None:
    snap = parse_runtime_snapshot(message([entry(thread_id=UUID)]), NOW)
    assert snap is not None
    assert (snap.entries[0].source, snap.entries[0].source_id) == ("codex", UUID)
    summary = source_summaries(snap, NOW)[0]
    assert summary.coverage == "unknown"
    assert "текущий счёт неизвестен" in summary.text


def test_codex_requires_uuid_but_other_sources_keep_native_id() -> None:
    assert parse_runtime_snapshot(message([entry(thread_id="local_abc")]), NOW) is None
    snap = parse_runtime_snapshot(
        message([entry(source="claude", source_id="local_abc")]), NOW)
    assert snap is not None
    assert snap.entries[0].source_id == "local_abc"
    assert snap.entries[0].thread_id == ""


def test_non_codex_without_id_rejected_but_unknown_source_has_none() -> None:
    assert parse_runtime_snapshot(message([entry(source="claude")]), NOW) is None
    snap = parse_runtime_snapshot(message([entry(source="unknown")]), NOW)
    assert snap is not None
    assert snap.entries[0].source_id == ""


def test_same_id_in_different_sources_is_not_a_duplicate_but_same_source_is() -> None:
    ok = [entry(thread_id=UUID), entry(source="claude", source_id=UUID)]
    assert parse_runtime_snapshot(message(ok), NOW) is not None
    twice = [entry(source="claude", source_id="a"), entry(source="claude", source_id="a")]
    assert parse_runtime_snapshot(message(twice), NOW) is None


def test_zero_only_claimed_with_complete_coverage() -> None:
    seen = "2026-10-14T11:55:00+00:00"
    sources = {"codex": {"coverage": "partial", "observed_at": seen},
               "claude": {"coverage": "complete", "observed_at": seen},
               "orch": {"coverage": "unavailable", "observed_at": seen}}
    snap = parse_runtime_snapshot(message([entry(source="claude", source_id="x",
                                                 state="completed")], sources), NOW)
    texts = {s.source: s.text for s in source_summaries(snap, NOW)}
    assert "0 активных (покрытие полное)" in texts["claude"]
    assert "не подтверждены" in texts["codex"]
    assert "недоступен" in texts["orch"]


def test_bad_coverage_value_rejected() -> None:
    assert parse_runtime_snapshot(message([], {"codex": {"coverage": "all"}}), NOW) is None


def test_observed_at_after_checked_or_after_now_rejects_whole_message() -> None:
    def with_observed(value: str) -> SimpleNamespace:
        return message([entry(thread_id=UUID)],
                       {"codex": {"coverage": "complete", "observed_at": value}})

    assert parse_runtime_snapshot(with_observed("2026-10-14T12:30:00+00:00"), NOW) is None
    assert parse_runtime_snapshot(with_observed("bad"), NOW) is None
    assert parse_runtime_snapshot(with_observed("2026-10-14T11:59:00+00:00"), NOW) is not None
