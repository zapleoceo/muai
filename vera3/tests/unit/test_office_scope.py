"""Scope filter of the Office report: pure rules, runtime exemption, rendered note."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dashboard.office_hourly_service import build_report
from dashboard.office_hourly_view import report_body
from dashboard.office_runtime_service import (
    PREFIX,
    RuntimeEntry,
    RuntimeSnapshot,
    parse_runtime_snapshot,
)
from dashboard.office_scope import (
    ScopeConfig,
    apply_scope,
    config_from_env,
    mask_hidden_deps,
    scope_runtime,
)
from dashboard.office_status import StatusInfo
from vera_shared.room.attention import Attention

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
CFG = ScopeConfig()
H = timedelta(hours=1)


def row(task_id: str = "X-1", project: str = "Vera", *, lease: timedelta | None = None,
        progress: timedelta | None = None, holder: str | None = None,
        responsible: str | None = None, status: str = "open",
        waiting_until: timedelta | None = None,
        waiting_reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        room="main", task_id=task_id, title=task_id, project=project, status=status,
        priority=2, lease_holder=holder, responsible=responsible, next_action=None,
        note=None, waiting_reason=waiting_reason,
        waiting_until=NOW + waiting_until if waiting_until is not None else None,
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
    ("orchestra live lease no holder", row(project="Orchestra", lease=10 * H), False),
    ("orchestra our live lease", row(project="Orchestra", lease=10 * H, holder="claude"), True),
    ("foreign live lease, our responsible, fresh progress",
     row(project="Orchestra", lease=H, holder="alice", responsible="claude", progress=H),
     False),
    ("foreign expired lease, our responsible, fresh progress",
     row(project="Orchestra", lease=-H, holder="alice", responsible="claude", progress=H),
     True),
    ("foreign live lease", row(project="Orchestra", lease=10 * H, holder="alice"), False),
    ("our live lease blocked", row(project="Orchestra", lease=H, holder="claude",
                                   status="blocked"), False),
    ("our live lease paused status", row(project="Lamas", lease=H, holder="dot",
                                         status="paused"), False),
    ("our live lease waiting_until future",
     row(project="Orchestra", lease=H, holder="claude", waiting_until=H), False),
    ("our live lease waiting_until past",
     row(project="Orchestra", lease=H, holder="claude", waiting_until=-H), True),
    ("our progress with waiting_reason",
     row(project="Lamas", progress=H, holder="codex", waiting_reason="CI"), False),
    ("our progress not waiting", row(project="Lamas", progress=H, holder="codex"), True),
    ("project STEP alias", row(project="step"), False),
    ("project SIN alias", row(project="SIN"), False),
    ("SinHRM is not the SIN alias", row(project="SinHRM", lease=H, holder="claude"), True),
    ("SIN alias is exact not prefix", row(project="SINX"), True),
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


@pytest.mark.parametrize("control", ["paused", "blocked", "waiting"])
def test_latest_control_event_parks_our_lease(control: str) -> None:
    item = row("o1", "Orchestra", lease=H, holder="claude")
    amap = {("main", "o1"): control}
    assert apply_scope([item], NOW, CFG, amap).hidden_inactive == 1
    assert apply_scope([item], NOW, CFG, {("main", "o1"): "resumed"}).visible == [item]


def runtime_entry(project: str, source_id: str = "", thread_id: str = "") -> RuntimeEntry:
    return RuntimeEntry(project, thread_id, "active", "d", "broker", source_id)


@pytest.mark.parametrize(("entry", "shown"), [
    (runtime_entry("STEP", "STEP-5933"), False),
    (runtime_entry("Vera", "SIN-4878"), False),
    (runtime_entry("Vera", "step-1"), False),
    (runtime_entry("Vera", "", "SIN-9"), False),
    (runtime_entry("sin", "x-1"), False),
    (runtime_entry("SinHRM", "HRM-1"), True),
    (runtime_entry("Orchestra", "ORCH-607"), True),
])
def test_runtime_exact_sin_step_exclusion(entry: RuntimeEntry, shown: bool) -> None:
    snap = RuntimeSnapshot(NOW, NOW, "m", (entry,))
    kept, hidden = scope_runtime(snap, CFG)
    assert (len(kept.entries) == 1) is shown
    assert hidden == (0 if shown else 1)


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


def test_visible_card_does_not_leak_hidden_dependency_id() -> None:
    card = row("V1", "Vera", holder="dot", lease=H)
    card.depends_on = ["SIN-123", "V0"]
    card.waiting_until = None
    hidden = row("SIN-123")
    amap = {("main", "V1"): Attention("in_progress", "", None, None, None),
            ("main", "SIN-123"): Attention("unassigned", "", None, None, None)}
    report = build_report([card, hidden], amap, [], [], NOW)
    html = report_body(report)
    assert "SIN-123" not in html
    assert "скрыто фильтром Office — " not in html
    deps = report.cards[0].details.dependencies
    assert [d.task_id for d in deps] == ["", "V0"]
    assert deps[0].status == "скрыто фильтром Office"
    assert len(deps) == 2


def test_note_lists_active_only_projects_from_config() -> None:
    cfg = config_from_env({"OFFICE_ACTIVE_ONLY_PROJECTS": "Alpha, Beta"})
    rows = [row("a1", "Alpha")]
    amap = {("main", "a1"): Attention("unassigned", "", None, None, None)}
    html = report_body(build_report(rows, amap, [], [], NOW, scope=cfg))
    assert "не в работе в Alpha/Beta: 1" in html
    assert "Orchestra/Lamas" not in html


def test_dependency_mask_is_not_substring_replace() -> None:
    card = row("V1", "Vera", holder="dot", lease=H)
    card.depends_on = ["O1", "O12"]
    info = StatusInfo("Блокер", None, None, "зависит от: O1, O12; ждём до 2026-10-12 19:00",
                      False, None, "fresh")
    out = mask_hidden_deps(card, info, frozenset({("main", "O1")}))
    assert out.dependency == ("зависит от: скрыто фильтром Office, O12; "
                              "ждём до 2026-10-12 19:00")
    assert mask_hidden_deps(card, info, frozenset()) is info
