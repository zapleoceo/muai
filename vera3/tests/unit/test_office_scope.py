"""Scope filter of the Office report: pure rules, runtime exemption, rendered note."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dashboard.office_hourly_service import build_report
from dashboard.office_hourly_view import report_body
from dashboard.office_runtime_service import PREFIX, parse_runtime_snapshot
from dashboard.office_scope import (
    ScopeConfig,
    apply_scope,
    config_from_env,
    scope_runtime,
)
from vera_shared.room.attention import Attention

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
CFG = ScopeConfig()
H = timedelta(hours=1)


def row(task_id: str = "X-1", project: str = "Vera", *, lease: timedelta | None = None,
        progress: timedelta | None = None, holder: str | None = None,
        responsible: str | None = None, status: str = "open") -> SimpleNamespace:
    return SimpleNamespace(
        room="main", task_id=task_id, title=task_id, project=project, status=status,
        priority=2, lease_holder=holder, responsible=responsible, next_action=None,
        note=None, waiting_reason=None,
        lease_until=NOW + lease if lease is not None else None,
        last_progress_at=NOW - progress if progress is not None else None)


CASES = [
    ("sin prefix", row("SIN-1"), False),
    ("step prefix lower", row("step-9"), False),
    ("other prefix", row("SINX-1"), True),
    ("excluded project", row(project="Sintegrum"), False),
    ("excluded project casefold", row(project="ИИ-ЗВОНИЛКА"), False),
    ("plain project", row(), True),
    ("orchestra never claimed", row(project="Orchestra"), False),
    ("orchestra live lease", row(project="Orchestra", lease=10 * H), True),
    ("orchestra expired lease no progress", row(project="Orchestra", lease=-H), False),
    ("progress inside window agent holder",
     row(project="Lamas", progress=71 * H, holder="codex"), True),
    ("progress exactly at window", row(project="Lamas", progress=72 * H, holder="dot"), True),
    ("progress past window", row(project="Lamas", progress=72 * H + timedelta(seconds=1),
                                 holder="dot"), False),
    ("responsible Claude / Lamas",
     row(project="Lamas", progress=H, responsible="Claude / Lamas"), True),
    ("human responsible", row(project="SinHRM", progress=H,
                              responsible="Дмитрий Запорожец — Jira"), False),
    ("agent substring not word", row(project="SinHRM", progress=H, holder="dotty"), False),
    ("progress but no agent", row(project="Orchestra", progress=H), False),
    ("agent but no progress", row(project="Orchestra", holder="claude"), False),
]


@pytest.mark.parametrize(("name", "item", "shown"), CASES, ids=[c[0] for c in CASES])
def test_apply_scope_table(name: str, item: SimpleNamespace, shown: bool) -> None:
    split = apply_scope([item], NOW, CFG)
    assert (split.visible == [item]) is shown
    assert split.hidden_excluded + split.hidden_inactive == (0 if shown else 1)


def test_hidden_counts_split_and_skip_closed() -> None:
    rows = [row("SIN-1"), row(project="Sintegrum"), row("o1", "Orchestra"),
            row("SIN-2", status="done"), row("o2", "Orchestra", status="cancelled")]
    split = apply_scope(rows, NOW, CFG)
    assert (split.hidden_excluded, split.hidden_inactive, split.visible) == (2, 1, [])


def test_env_config_overrides_and_empty_disables() -> None:
    cfg = config_from_env({"OFFICE_EXCLUDE_PROJECTS": "", "OFFICE_EXCLUDE_PREFIXES": "",
                           "OFFICE_ACTIVE_ONLY_PROJECTS": "Vera",
                           "OFFICE_ACTIVE_WINDOW_HOURS": "1"})
    assert apply_scope([row("SIN-1"), row(project="Sintegrum")], NOW, cfg).hidden_excluded == 0
    assert apply_scope([row(progress=2 * H, holder="dot")], NOW, cfg).hidden_inactive == 1
    assert config_from_env({}) == CFG
    bad = config_from_env({"OFFICE_ACTIVE_WINDOW_HOURS": "bad"})
    assert bad.active_window == timedelta(hours=72)


def runtime_snapshot():
    body = PREFIX + json.dumps({"checked_at": NOW.isoformat(), "entries": [
        {"project": "Orchestra", "source": "broker", "source_id": "ORCH-607",
         "state": "active", "detail": "Orchestra runtime work"},
        {"project": "Sintegrum", "source": "broker", "source_id": "SIN-1",
         "state": "active", "detail": "Sintegrum runtime work"}],
        "sources": {"broker": {"coverage": "complete", "observed_at": NOW.isoformat()}}})
    msg = SimpleNamespace(room="main", from_agent="dot", to_agent="office-dashboard",
                          task_id=None, status="info", body=body, created_at=NOW,
                          message_id="m1")
    snap = parse_runtime_snapshot(msg, NOW)
    assert snap is not None
    return snap


def test_runtime_orchestra_stays_sintegrum_hidden_while_room_cards_hidden() -> None:
    card = row("o1", "Orchestra")
    report = build_report([card], {("main", "o1"): Attention("unassigned", "", None, None, None)},
                          [], [], NOW)
    assert report.cards == [] and report.hidden_inactive == 1
    snap, hidden = scope_runtime(runtime_snapshot(), CFG)
    assert hidden == 1 and [e.project for e in snap.entries] == ["Orchestra"]
    html = report_body(replace(report, runtime=snap, hidden_runtime=hidden))
    assert "Orchestra runtime work" in html
    assert "Sintegrum runtime work" not in html
    assert "Подтверждённые активные потоки: 1" in html


def test_scope_note_rendered_and_absent_when_nothing_hidden() -> None:
    rows = [row("SIN-1"), row("o1", "Orchestra"), row("o2", "Lamas")]
    amap = {("main", r.task_id): Attention("unassigned", "", None, None, None) for r in rows}
    html = report_body(replace(build_report(rows, amap, [], [], NOW), hidden_runtime=2))
    assert ("Скрыто фильтром Office: 5 (SIN/STEP и проекты вне мониторинга: 1; "
            "не в работе в Orchestra/Lamas/SinHRM: 2; внешние runtime вне мониторинга: 2)") in html
    assert "Скрыто фильтром" not in report_body(build_report([], {}, [], [], NOW))
