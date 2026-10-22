"""Evidence and counting rules for the owner hourly report."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from dashboard.office_hourly_service import (
    SCOPE_PREFIX,
    build_report,
    next_question_reminder,
    parse_scope_decisions,
)
from dashboard.office_hourly_view import modal_body, report_body
from dashboard.office_runtime_service import (
    PREFIX,
    latest_runtime_snapshot,
    parse_runtime_snapshot,
)
from vera_shared.room.attention import Attention

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)


def task(task_id: str, status: str = "open", *, holder: str | None = None,
         project: str = "Vera") -> SimpleNamespace:
    return SimpleNamespace(room="main", task_id=task_id, title=task_id,
                           project=project, status=status, priority=2,
                           lease_holder=holder, lease_until=NOW + timedelta(minutes=10)
                           if holder else None, responsible=None, next_action="Следующий шаг",
                           note="Отменено владельцем", last_progress_at=None,
                           waiting_reason=None)


def attention(state: str) -> Attention:
    return Attention(state, state, None, None, None)


def event(task_id: str, kind: str, minutes_ago: int, text: str = "Результат") -> SimpleNamespace:
    return SimpleNamespace(room="main", task_id=task_id, kind=kind, agent="dot",
                           at=NOW - timedelta(minutes=minutes_ago), id=minutes_ago,
                           text=text, data={"next_checkpoint_seconds": None}
                           if kind == "progress" else None)


def test_statuses_sum_once_and_done_is_hidden() -> None:
    rows = [task("queue"), task("active", holder="dot"), task("blocked", "blocked"),
            task("review"), task("done", "done")]
    states = ["unassigned", "in_progress", "blocked", "unassigned", "done"]
    amap = {(r.room, r.task_id): attention(s) for r, s in zip(rows, states, strict=True)}
    report = build_report(rows, amap, [], [event("done", "done", 10)], NOW,
                          [event("review", "review", 5)])
    assert sum(report.counts.values()) == len(rows) - 1
    assert len(report.cards) == 4
    assert report.counts == {"Новая / в очереди": 1, "В работе": 1, "Блокер": 1, "Ревью": 1}
    assert report.results == []
    html = report_body(report)
    assert 'aria-label="Готово"' in html
    assert "done</a>" not in html


def test_hour_delta_uses_event_time_and_result_kinds_only() -> None:
    row = task("one", holder="dot")
    amap = {(row.room, row.task_id): attention("in_progress")}
    events = [event("one", "progress", 15, "Файл проверен"),
              event("one", "progress", 70, "Старый результат"),
              event("one", "metadata_updated", 5, "Учётная коррекция"),
              event("one", "claimed", 3), event("one", "progress", 2, "")]
    events.append(SimpleNamespace(room="main", task_id="one", kind="progress",
                                  at=NOW - timedelta(minutes=1), id=100,
                                  text="status: blocked", data={"status": "blocked"}))
    report = build_report([row], amap, [], events, NOW)
    assert [e.text for e in report.results] == ["Файл проверен"]
    assert report.cards[0].last_confirmed.text == "Файл проверен"


def test_open_question_and_six_hour_checkpoint() -> None:
    row = task("q", holder="dot")
    amap = {(row.room, row.task_id): attention("needs_owner")}
    q = SimpleNamespace(room="main", task_id="q", status="open",
                        question="Какой вариант выбрать?", asked_at=NOW - timedelta(hours=7))
    report = build_report([row], amap, [q], [], NOW)
    assert report.cards[0].status == "Блокер"
    assert next_question_reminder(q, NOW) == NOW + timedelta(hours=5)
    assert "Какой вариант выбрать?" in report_body(report)


def test_answered_hold_is_review_without_reasking() -> None:
    row = task("answered", "blocked")
    amap = {(row.room, row.task_id): attention("answered")}
    report = build_report([row], amap, [], [], NOW)
    assert report.cards[0].status == "Ревью"
    assert "Следующий шестичасовой контроль" not in report_body(report)


def test_cancelled_and_done_are_absent_from_the_report() -> None:
    rows = [task("cancelled", "cancelled"), task("finished", "done")]
    amap = {(r.room, r.task_id): attention(r.status) for r in rows}
    events = [event("cancelled", "progress", 4), event("finished", "done", 4)]
    report = build_report(rows, amap, [], events, NOW)
    assert report.cards == []
    assert report.results == []
    assert sum(report.counts.values()) == 0
    html = report_body(report)
    assert 'aria-label="Отменена"' in html
    assert 'data-detail="/office/hourly/detail/main/cancelled"' not in html
    assert "Отменено владельцем" not in html
    assert "cancelled</a>" not in html
    assert "finished</a>" not in html


def test_review_event_and_pause_are_distinct() -> None:
    review, pause = task("review"), task("pause")
    rows = [review, pause]
    amap = {(r.room, r.task_id): attention("unassigned") for r in rows}
    controls = [event("review", "review", 2), event("pause", "paused", 2)]
    report = build_report(rows, amap, [], [], NOW, controls)
    assert [c.status for c in report.cards] == ["Блокер", "Ревью"]
    assert 'aria-label="Блокер"' in report_body(report)
    assert 'aria-label="На проверке"' in report_body(report)


def test_acknowledged_question_or_new_claim_ends_old_review_control() -> None:
    rows = [task("answered", "in_progress", holder="dot"), task("pause")]
    amap = {(r.room, r.task_id): attention("in_progress") for r in rows}
    controls = [event("answered", "ack_answer", 1), event("pause", "claimed", 1)]
    report = build_report(rows, amap, [], [], NOW, controls)
    assert report.counts["Ревью"] == 0
    assert report.counts["В работе"] == 1
    assert report.counts["Новая / в очереди"] == 1


def runtime_message(*, actor: str = "dot", checked: datetime = NOW) -> SimpleNamespace:
    body = PREFIX + json.dumps({"checked_at": checked.isoformat(), "entries": [
        {"project": "Orchestra", "thread_id": "01a12abb-b3b4-74dc-aeeb-3432e9fd509f",
         "state": "active", "detail": "Исправление для superadmin выполняется"},
        {"project": "Vera", "thread_id": "01a12abc-f65c-72f9-a5ed-255fb375e0b8",
         "state": "active", "detail": "Office dashboard выполняется"},
        {"project": "Broker", "thread_id": "01a12aba-d455-71b0-a2be-adb2edb4b0b0",
         "state": "completed", "detail": "PR62 завершён"},
    ]})
    return SimpleNamespace(room="main", from_agent=actor, to_agent="office-dashboard",
                           task_id=None, status="info", body=body, created_at=NOW,
                           message_id="office-runtime-test")


def test_external_runtime_is_separate_from_room_statuses() -> None:
    row = task("queued")
    report = build_report([row], {(row.room, row.task_id): attention("unassigned")},
                          [], [], NOW)
    snapshot = parse_runtime_snapshot(runtime_message(), NOW)
    assert snapshot is not None
    html = report_body(replace(report, runtime=snapshot))
    assert "Подтверждённые активные потоки" not in html
    assert "Текущий счёт неизвестен" in html
    assert "Историческое наблюдение на" in html
    assert "0 подтверждено" not in html
    assert "Room «В работе» = 0 живых lease" in html
    assert "Room lease: 0 · проверенные runtime в работе: не подтверждено" in html
    assert "Этап завершён" in html
    assert report.counts["В работе"] == 0


def test_completed_only_legacy_project_is_not_proven_zero() -> None:
    row = task("broker-queued", project="Broker")
    report = build_report([row], {(row.room, row.task_id): attention("unassigned")},
                          [], [], NOW)
    snapshot = parse_runtime_snapshot(runtime_message(), NOW)
    assert snapshot is not None
    html = report_body(replace(report, runtime=snapshot))
    assert "Room lease: 0 · проверенные runtime в работе: не подтверждено" in html


def test_runtime_snapshot_rejects_wrong_actor_and_expires_without_current_count() -> None:
    assert parse_runtime_snapshot(runtime_message(actor="claude"), NOW) is None
    old = NOW - timedelta(minutes=61)
    row = runtime_message(checked=old)
    row.created_at = old
    snapshot = parse_runtime_snapshot(row, NOW)
    assert snapshot is not None and not snapshot.is_fresh(NOW)
    report = build_report([], {}, [], [], NOW)
    html = report_body(replace(report, runtime=snapshot))
    assert "текущий счёт неизвестен" in html
    assert "Подтверждённые активные потоки: 2" not in html


def test_runtime_snapshot_rejects_duplicate_thread_and_escapes_details() -> None:
    row = runtime_message()
    payload = json.loads(row.body[len(PREFIX):])
    payload["entries"].append(payload["entries"][0])
    row.body = PREFIX + json.dumps(payload)
    assert parse_runtime_snapshot(row, NOW) is None
    payload["entries"].pop()
    payload["entries"][0]["detail"] = "<script>alert(1)</script>"
    row.body = PREFIX + json.dumps(payload)
    snapshot = parse_runtime_snapshot(row, NOW)
    assert snapshot is not None
    html = report_body(replace(build_report([], {}, [], [], NOW), runtime=snapshot))
    assert "<script>alert(1)</script>" not in html


def test_newer_check_wins_when_older_observation_arrives_later() -> None:
    recent = runtime_message(checked=NOW - timedelta(minutes=5))
    recent.created_at = NOW - timedelta(minutes=4)
    recent.message_id = "recent-check"
    late_old = runtime_message(checked=NOW - timedelta(minutes=40))
    late_old.created_at = NOW
    late_old.message_id = "late-old-check"
    snapshot = latest_runtime_snapshot([late_old, recent], NOW)
    assert snapshot is not None and snapshot.message_id == "recent-check"


def test_blocker_label_shows_confidence() -> None:
    blocked, asked = task("blocked", "blocked"), task("asked", holder="dot")
    amap = {(r.room, r.task_id): attention("in_progress") for r in (blocked, asked)}
    q = SimpleNamespace(room="main", task_id="asked", status="open", question="Вопрос?",
                        asked_at=NOW - timedelta(hours=1))
    report = build_report([blocked, asked], amap, [q], [], NOW)
    assert report.counts["Блокер"] == 2
    html = report_body(report)
    assert "Основание блокера: не подтверждён" in html
    assert "Основание блокера: подтверждён" in html


def test_kanban_six_columns_and_explicit_closed_filter() -> None:
    rows = [task("active", holder="dot"), task("finished", "done"),
            task("cancelled", "cancelled")]
    amap = {(r.room, r.task_id): attention(r.status) for r in rows}
    default = build_report(rows, amap, [], [], NOW)
    html = report_body(default)
    assert html.count('class="kb-col"') == 6
    assert 'aria-label="На проверке"' in html
    assert 'data-detail="/office/hourly/detail/main/active"' in html
    assert 'data-detail="/office/hourly/detail/main/finished"' not in html
    assert 'data-detail="/office/hourly/detail/main/cancelled"' not in html
    shown = build_report(rows, amap, [], [], NOW, include_closed=True)
    shown_html = report_body(shown, show_closed=True)
    assert shown.counts["Готово"] == shown.counts["Отменена"] == 1
    assert 'data-detail="/office/hourly/detail/main/finished"' in shown_html
    assert 'data-detail="/office/hourly/detail/main/cancelled"' in shown_html
    assert 'href="/office/hourly?show_closed=true" aria-current="page"' in shown_html


def test_owner_cancel_scope_uses_attributed_room_record_without_changing_raw_status() -> None:
    row = task("studio-balcony-model")
    decisions = {("main", row.task_id): "room:3867"}
    raw = SimpleNamespace(room="main", from_agent="dot", to_agent="office-dashboard",
                          task_id=None, status="info", body=SCOPE_PREFIX + json.dumps({
                              "pending_owner_cancellations": [
                                  {"room": "main", "task_id": row.task_id,
                                   "source": "room:3867"}]}))
    assert parse_scope_decisions(raw) == decisions
    raw.from_agent = "claude"
    assert parse_scope_decisions(raw) is None
    raw.from_agent = "dot"
    raw.body = SCOPE_PREFIX + '{"pending_owner_cancellations": [{"room": "main", "task_id": "x", "source": "arbitrary"}]}'
    assert parse_scope_decisions(raw) is None
    report = build_report([row], {(row.room, row.task_id): attention("unassigned")},
                          [], [], NOW, pending_decisions=decisions,
                          scope_message_id="scope-check")
    assert report.cards == [] and row.status == "open"
    assert report.pending_cancellations == [(row, "room:3867")]
    html = report_body(report)
    assert "Отмена владельца ожидает записи в Room: 1" in html
    assert "запись Room: open" in html
    row.status = "cancelled"
    closed = build_report([row], {(row.room, row.task_id): attention("cancelled")},
                          [], [], NOW, include_closed=True, pending_decisions=decisions,
                          scope_message_id="scope-check")
    assert closed.pending_cancellations == []
    assert closed.counts["Отменена"] == 1


def test_modal_is_read_only_and_escapes_task_history() -> None:
    row = task("modal", holder="dot")
    row.note = "Описание <script>alert(1)</script>"
    row.refs = [{"kind": "source", "ref": "<img onerror=alert(2)>"}]
    row.depends_on = ["other"]
    row.last_progress_at = NOW
    row.last_progress_text = "Результат готов"
    row.waiting_reason = "Ждём проверку"
    q = SimpleNamespace(question="<owner?>", status="open")
    ev = event("modal", "progress", 3, "<bad> событие")
    html = modal_body(SimpleNamespace(row=row), [ev], [(q, [])])
    assert "Описание" in html and "Следующий шаг" in html
    assert "Результат готов" in html and "История" in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "&lt;bad&gt;" in html and "<bad>" not in html
    assert "<form" not in html
