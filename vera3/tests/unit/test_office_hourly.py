"""Evidence and counting rules for the owner hourly report."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from dashboard.office_hourly_service import build_report, next_question_reminder
from dashboard.office_hourly_view import report_body
from vera_shared.room.attention import Attention

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)


def task(task_id: str, status: str = "open", *, holder: str | None = None,
         project: str = "Vera") -> SimpleNamespace:
    return SimpleNamespace(room="main", task_id=task_id, title=task_id,
                           project=project, status=status, priority=2,
                           lease_holder=holder, lease_until=NOW + timedelta(minutes=10)
                           if holder else None, responsible=None, next_action="Следующий шаг",
                           note="Отменено владельцем")


def attention(state: str) -> Attention:
    return Attention(state, state, None, None, None)


def event(task_id: str, kind: str, minutes_ago: int, text: str = "Результат") -> SimpleNamespace:
    return SimpleNamespace(room="main", task_id=task_id, kind=kind,
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
    assert sum(report.counts.values()) == len(rows)
    assert len(report.cards) == 4
    assert report.counts == {"Новая / в очереди": 1, "В работе": 1, "Блокер": 1,
                             "На проверке": 1, "Готово": 1, "Отменена": 0}
    assert report.results == []
    html = report_body(report)
    assert "Готово</span>" not in html
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
    assert report.cards[0].status == "На проверке"
    assert "Следующий шестичасовой контроль" not in report_body(report)


def test_cancelled_is_separate_from_done_and_active_wip() -> None:
    row = task("cancelled", "cancelled")
    amap = {(row.room, row.task_id): attention("cancelled")}
    report = build_report([row], amap, [], [event("cancelled", "progress", 4)], NOW)
    assert report.counts["Отменена"] == 1
    assert report.counts["В работе"] == 0
    assert report.results == []
    assert "Отменено владельцем" in report_body(report)


def test_review_event_and_pause_are_distinct() -> None:
    review, pause = task("review"), task("pause")
    rows = [review, pause]
    amap = {(r.room, r.task_id): attention("unassigned") for r in rows}
    controls = [event("review", "review", 2), event("pause", "paused", 2)]
    report = build_report(rows, amap, [], [], NOW, controls)
    assert [c.status for c in report.cards] == ["Новая / в очереди", "На проверке"]
    assert "Пауза зафиксирована" in report_body(report)


def test_acknowledged_question_or_new_claim_ends_old_review_control() -> None:
    rows = [task("answered", "in_progress", holder="dot"), task("pause")]
    amap = {(r.room, r.task_id): attention("in_progress") for r in rows}
    controls = [event("answered", "ack_answer", 1), event("pause", "claimed", 1)]
    report = build_report(rows, amap, [], [], NOW, controls)
    assert report.counts["На проверке"] == 0
    assert report.counts["В работе"] == 1
    assert report.counts["Новая / в очереди"] == 1
