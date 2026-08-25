"""Очередь на диске: дозапись, закрытие, восстановление после падения."""
from __future__ import annotations

from vera_listener.outbox import Outbox, read_payload


def _outbox(tmp_path) -> Outbox:
    return Outbox(tmp_path / "queue")


def test_session_roundtrip(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s1", "2026-08-25T10:00:00", app="zoom.exe",
                     window_title="Коля — Zoom", device_hint="наушники")
    box.append(path, 1.0, "mic", "привет")
    box.append(path, 2.5, "system", "привет, слышу")
    ready = box.finish(path, "2026-08-25T10:05:00")

    assert box.ready() == [ready]
    payload = read_payload(ready)
    assert payload["started_at"] == "2026-08-25T10:00:00"
    assert payload["ended_at"] == "2026-08-25T10:05:00"
    assert payload["app"] == "zoom.exe"
    assert [u["stream"] for u in payload["utterances"]] == ["mic", "system"]


def test_empty_utterances_are_not_written(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s2", "2026-08-25T10:00:00", app=None,
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "mic", "   ")
    assert read_payload(path) is None


def test_crashed_session_is_recovered_with_derived_end(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s3", "2026-08-25T10:00:00", app="zoom.exe",
                     window_title=None, device_hint=None)
    box.append(path, 42.0, "mic", "успели записать")
    # Футера нет — процесс упал. Файл старый, значит сессия не живая.
    import os
    old = path.stat().st_mtime - 7200
    os.utime(path, (old, old))

    moved = box.recover(max_age_s=3600.0)
    assert len(moved) == 1
    payload = read_payload(moved[0])
    assert payload["ended_at"] == "2026-08-25T10:00:42"


def test_live_session_is_not_recovered(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s4", "2026-08-25T10:00:00", app=None,
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "mic", "идёт прямо сейчас")
    assert box.recover(max_age_s=3600.0) == []
    assert path.exists()


def test_torn_last_line_does_not_lose_the_rest(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s5", "2026-08-25T10:00:00", app=None,
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "mic", "первая")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "u", "at": 2.0, "st')

    payload = read_payload(path)
    assert len(payload["utterances"]) == 1


def test_parked_file_leaves_the_queue(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s6", "2026-08-25T10:00:00", app=None,
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "mic", "текст")
    ready = box.finish(path, "2026-08-25T10:01:00")
    box.park(ready, "тест")
    assert box.ready() == []
    assert (box.failed_dir / "s6.jsonl").exists()


def test_finish_can_replace_utterances_after_echo_cleanup(tmp_path):
    box = Outbox(tmp_path / "queue")
    path = box.start("s7", "2026-08-25T10:00:00", app="zoom.exe",
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "system", "давай в четверг")
    box.append(path, 1.2, "mic", "давай в четверг")
    ready = box.finish(path, "2026-08-25T10:01:00",
                       utterances=[{"at": 1.0, "stream": "system",
                                    "text": "давай в четверг"}])
    payload = read_payload(ready)
    assert len(payload["utterances"]) == 1
    assert payload["utterances"][0]["stream"] == "system"
    assert payload["app"] == "zoom.exe"
