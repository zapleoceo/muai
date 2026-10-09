"""Интервалы выполнения из журнала и SVG-диаграммы Ганта `/tasks`."""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard.tasks_gantt import (  # noqa: E402
    GanttRow,
    parse_span,
    task_gantt,
    tasks_gantt,
)
from vera_shared.room.intervals import Segment, build_segments  # noqa: E402

T0 = datetime(2026, 10, 9, 8, 0)
NOW = datetime(2026, 10, 9, 12, 0)


def ev(minutes, kind, agent="claude", session="s1", data=None):
    return SimpleNamespace(at=T0 + timedelta(minutes=minutes), kind=kind, agent=agent,
                           session=session, data=data)


def spans(segs):
    return [(s.kind, int((s.start - T0).total_seconds() // 60),
             int((s.end - T0).total_seconds() // 60)) for s in segs]


def test_claim_progress_release():
    segs = build_segments([ev(0, "claimed"), ev(10, "progress"), ev(30, "released")], now=NOW)
    assert spans(segs) == [("work", 0, 30)]


def test_pause_resume():
    segs = build_segments([ev(0, "claimed"), ev(10, "paused"), ev(20, "progress"),
                           ev(30, "resumed"), ev(40, "done")], now=NOW)
    assert spans(segs) == [("work", 0, 10), ("paused", 10, 30), ("work", 30, 40)]


def test_question_until_ack():
    segs = build_segments([ev(0, "claimed"), ev(10, "question"), ev(15, "answered", agent=None),
                           ev(25, "ack_answer"), ev(25, "unblocked"), ev(30, "released")], now=NOW)
    assert spans(segs) == [("work", 0, 10), ("blocked", 10, 25), ("work", 25, 30)]


def test_lease_expired_without_release():
    segs = build_segments([ev(0, "claimed"), ev(5, "heartbeat"),
                           ev(60, "lease_expired", agent=None)], now=NOW)
    assert spans(segs) == [("work", 0, 60)]


def test_open_live_lease_ends_at_now():
    segs = build_segments([ev(0, "claimed")], now=NOW, lease_until=NOW + timedelta(hours=1))
    assert segs[0].end == NOW


def test_open_dead_lease_ends_at_lease_until():
    events = [ev(0, "claimed"), ev(10, "heartbeat")]
    segs = build_segments(events, now=NOW, lease_until=T0 + timedelta(minutes=40))
    assert spans(segs) == [("work", 0, 40)]
    assert spans(build_segments(events, now=NOW)) == [("work", 0, 10)]


def test_no_events_no_bars():
    assert build_segments([], now=NOW, lease_until=NOW + timedelta(hours=1)) == []
    assert "Интервалов" in task_gantt([], None)


def test_waiting_cap_then_unknown_gap():
    segs = build_segments([ev(0, "claimed"), ev(5, "waiting", data={"until_seconds": 600}),
                           ev(60, "progress"), ev(70, "released")], now=NOW)
    assert spans(segs) == [("work", 0, 5), ("waiting", 5, 15), ("unknown", 15, 60),
                           ("work", 60, 70)]


def test_new_claim_closes_other_lane():
    segs = build_segments([ev(0, "claimed"), ev(20, "claimed", agent="codex", session="s2"),
                           ev(30, "released", agent="codex", session="s2")], now=NOW)
    assert [(s.agent, s.start, s.end) for s in segs] == [
        ("claude", T0, T0 + timedelta(minutes=20)),
        ("codex", T0 + timedelta(minutes=20), T0 + timedelta(minutes=30))]


def test_plan_drawn_separately_and_hollow():
    plan = (T0, T0 + timedelta(hours=2))
    html = task_gantt([Segment("claude", "s1", "work", T0, T0 + timedelta(hours=1))], plan)
    assert 'fill="none" stroke="var(--muted)"' in html and "план" in html
    assert 'fill="var(--ok)"' in html
    only_plan = task_gantt([], plan)
    assert 'fill="var(--ok)"' not in only_plan and "план" in only_plan


def test_svg_escaping():
    seg = Segment("<img src=x onerror=1>", '"s"', "work", T0, T0 + timedelta(hours=1))
    html = task_gantt([seg], None)
    assert "<img" not in html and "&lt;img" in html
    multi = tasks_gantt([GanttRow("<b>x</b>", [seg])], T0 + timedelta(hours=2), "24h", "work")
    assert "<b>x</b>" not in multi and "&lt;b&gt;" in multi


def test_span_param_and_legend():
    assert parse_span("7d") == "7d" and parse_span("24h") == "24h"
    assert parse_span("1y") == "24h" and parse_span(None) == "24h" and parse_span("") == "24h"
    seg = Segment("a", "", "paused", NOW - timedelta(hours=1), NOW)
    html = tasks_gantt([GanttRow("t", [seg], (NOW - timedelta(hours=2), NOW))],
                       NOW, "bogus", "me")
    assert "span=7d" in html and "tab=me" in html
    for word in ("работа", "пауза", "проверка", "блок", "ожидание", "неизвестно", "план"):
        assert word in html
    assert "интервалов нет" in tasks_gantt([GanttRow("t", [])], NOW, "24h", "work")


def test_update_blocked_sequence_draws_block_not_work():
    segs = build_segments([ev(0, "claimed"), ev(10, "blocked"), ev(10, "progress"),
                           ev(20, "unblocked"), ev(20, "progress"), ev(30, "released")], now=NOW)
    assert spans(segs) == [("work", 0, 10), ("blocked", 10, 20), ("work", 20, 30)]
