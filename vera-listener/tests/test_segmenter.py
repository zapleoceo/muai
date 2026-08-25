"""Нарезка разговоров: открытие, тишина, смена приложения, предохранитель."""
from __future__ import annotations

from vera_listener.segmenter import Segmenter


def _seg(**kw) -> Segmenter:
    return Segmenter(silence_timeout_s=60.0, max_session_s=7200.0, frame_s=1.0, **kw)


def test_session_opens_on_first_speech():
    seg = _seg()
    assert seg.feed(0.0, "mic", False) is None
    assert seg.current is None
    seg.feed(1.0, "mic", True, app="zoom.exe", window_title="Коля — Zoom")
    assert seg.current is not None
    assert seg.current.app == "zoom.exe"
    assert seg.current.window_title == "Коля — Zoom"


def test_silence_closes_session_at_last_speech():
    seg = _seg()
    seg.feed(10.0, "mic", True)
    seg.feed(11.0, "system", True)
    assert seg.feed(40.0, "mic", False) is None
    closed = seg.feed(71.5, "mic", False)
    assert closed is not None
    assert closed.reason == "silence"
    assert closed.ended_at == 11.0
    assert closed.session.speech_s == {"mic": 1.0, "system": 1.0}
    assert seg.current is None


def test_window_title_change_does_not_split_a_call():
    seg = _seg()
    seg.feed(0.0, "mic", True, app="zoom.exe", window_title="Zoom")
    # alt-tab посреди созвона: окно другое, приложение то же — сессия одна.
    assert seg.feed(1.0, "system", True, app="zoom.exe", window_title="Почта") is None
    assert seg.current is not None
    assert seg.current.started_at == 0.0


def test_app_change_starts_a_new_session():
    seg = _seg()
    seg.feed(0.0, "mic", True, app="zoom.exe")
    closed = seg.feed(5.0, "system", True, app="telegram.exe")
    assert closed is not None
    assert closed.reason == "app_changed"
    assert seg.current is not None and seg.current.app == "telegram.exe"


def test_app_learned_after_session_started():
    seg = _seg()
    seg.feed(0.0, "mic", True, app=None, window_title=None)
    seg.feed(1.0, "mic", True, app="zoom.exe", window_title="Созвон")
    assert seg.current.app == "zoom.exe"
    assert seg.current.window_title == "Созвон"


def test_endless_session_is_cut_by_the_guard():
    seg = Segmenter(silence_timeout_s=600.0, max_session_s=100.0, frame_s=1.0)
    seg.feed(0.0, "system", True, app="zoom.exe")
    seg.feed(90.0, "system", True, app="zoom.exe")
    closed = seg.feed(101.0, "system", True, app="zoom.exe")
    assert closed is not None and closed.reason == "max_duration"


def test_flush_closes_open_session_on_shutdown():
    seg = _seg()
    seg.feed(3.0, "mic", True)
    closed = seg.flush(9.0)
    assert closed is not None and closed.reason == "shutdown"
    assert seg.flush(10.0) is None
