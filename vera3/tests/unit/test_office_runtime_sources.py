"""OFFICE_RUNTIME_V1 backward-compatible source extension and coverage-aware counters."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from dashboard.office_runtime_service import PREFIX, parse_runtime_snapshot
from dashboard.office_runtime_summary import source_summaries
from vera_shared.office.runtime_snapshot import (
    snapshot_error,
    validate_runtime_publication,
)

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


def test_newer_snapshot_with_565_character_detail_is_accepted() -> None:
    old = message([entry(thread_id=UUID)])
    old.message_id = "dot-office-runtime-older"
    old.body = old.body.replace("2026-10-14T12:00:00+00:00", "2026-10-14T11:59:00+00:00")
    newer = message([entry(thread_id=UUID, detail="x" * 565)])
    newer.message_id = "dot-office-runtime-20261021T152900Z"
    from dashboard.office_runtime_service import latest_runtime_snapshot
    picked = latest_runtime_snapshot([old, newer], NOW)
    assert picked is not None and picked.message_id == newer.message_id
    assert parse_runtime_snapshot(message([entry(thread_id=UUID, detail="x" * 1001)]), NOW) is None


@pytest.mark.parametrize(("bad", "reason"), [
    (entry(thread_id=UUID, state="in_progress"), "entries[0].state"),
    (entry(source="codex"), "entries[0].thread_id"),
    (entry(thread_id=UUID, detail="x" * 1001), "entries[0].detail"),
])
def test_invalid_runtime_post_reports_field_without_persisting(bad: dict,
                                                                reason: str) -> None:
    row = message([bad])
    assert snapshot_error(row, NOW).startswith(reason)
    with pytest.raises(ValueError, match=reason.replace("[", r"\[").replace("]", r"\]")):
        validate_runtime_publication(row.body, NOW)


def test_mixed_valid_and_invalid_entries_reject_whole_post() -> None:
    row = message([entry(thread_id=UUID), entry(source="codex", state="waiting")])
    assert parse_runtime_snapshot(row, NOW) is None
    assert snapshot_error(row, NOW).startswith("entries[1].thread_id")


def test_valid_native_id_and_coverage_pass_publication() -> None:
    row = message([entry(source="claude", source_id="local_abc")],
                  {"claude": {"coverage": "partial", "observed_at": NOW.isoformat()}})
    validate_runtime_publication(row.body, NOW)


def test_bad_coverage_reports_source_field() -> None:
    row = message([], {"codex": {"coverage": "scoped"}})
    assert snapshot_error(row, NOW) == (
        "sources.codex.coverage must be partial, complete or unavailable")


def test_newer_invalid_snapshot_cannot_resurrect_older_count() -> None:
    from dashboard.office_runtime_service import latest_runtime_snapshot

    older = message([entry(thread_id=UUID)])
    older.message_id = "older"
    older.body = older.body.replace("2026-10-14T12:00:00+00:00",
                                    "2026-10-14T11:59:00+00:00")
    newer = message([entry(thread_id=UUID, state="in_progress")])
    newer.message_id = "newer"
    assert latest_runtime_snapshot([newer, older], NOW) is None


def test_late_invalid_older_check_does_not_hide_newer_valid_check() -> None:
    from dashboard.office_runtime_service import latest_runtime_snapshot

    current = message([entry(thread_id=UUID)])
    current.message_id = "current"
    late = message([entry(thread_id=UUID, state="in_progress")])
    late.message_id = "late"
    late.body = late.body.replace("2026-10-14T12:00:00+00:00",
                                  "2026-10-14T11:58:00+00:00")
    late.created_at = NOW.replace(minute=1)
    picked = latest_runtime_snapshot([late, current], NOW)
    assert picked is not None and picked.message_id == "current"


def test_future_invalid_check_does_not_hide_later_valid_correction() -> None:
    from dashboard.office_runtime_service import latest_runtime_snapshot

    future_invalid = message([entry(thread_id=UUID, state="in_progress")])
    future_invalid.message_id = "future-invalid"
    future_invalid.body = future_invalid.body.replace(
        "2026-10-14T12:00:00+00:00", "2099-01-01T00:00:00+00:00")
    corrected = message([entry(thread_id=UUID)])
    corrected.message_id = "corrected"
    corrected.created_at = NOW.replace(minute=5)
    corrected.body = corrected.body.replace(
        "2026-10-14T12:00:00+00:00", "2026-10-14T12:05:00+00:00")
    picked = latest_runtime_snapshot([future_invalid, corrected], corrected.created_at)
    assert picked is not None and picked.message_id == "corrected"
