"""Склейка длинной встречи и настраиваемость предохранителя.

Предохранитель по длительности режет трёхчасовую встречу на части. Части
обязаны нести общий meeting_id: иначе в мозге это два независимых события,
и вторая половина начинается с середины фразы без всякой связи с первой.
"""
from __future__ import annotations

from vera_listener.config import Config, load_config
from vera_listener.outbox import Outbox, read_payload
from vera_listener.segmenter import Segmenter


def _closed(reason: str):
    """Тишина проверяется раньше предохранителя, поэтому окно тишины должно
    быть шире шага, иначе сессия закроется не по той причине."""
    seg = Segmenter(silence_timeout_s=10.0, max_session_s=2.0, frame_s=0.5)
    seg.feed(0.0, "mic", True)
    seg.feed(1.0, "mic", True)
    if reason == "max_duration":
        return seg.feed(3.0, "mic", True)
    return seg.feed(20.0, "mic", False)


class TestSegmenterReasons:
    def test_duration_guard_closes_with_max_duration(self):
        closed = _closed("max_duration")
        assert closed is not None and closed.reason == "max_duration"

    def test_silence_closes_with_silence(self):
        closed = _closed("silence")
        assert closed is not None and closed.reason == "silence"

    def test_guard_is_configurable_from_env(self, monkeypatch):
        """До 2026-08-26 max_session_s не читался из окружения вовсе: потолок
        в два часа нельзя было подкрутить, не правя код."""
        monkeypatch.setenv("VERA_MAX_SESSION_S", "10800")
        monkeypatch.setenv("VERA_CHUNK_SPEECH_S", "90")
        monkeypatch.setenv("VERA_SEND_INTERVAL_S", "45")
        assert load_config().max_session_s == 10800.0
        assert load_config().chunk_speech_s == 90.0
        assert load_config().send_interval_s == 45.0

    def test_defaults_hold_without_env(self):
        assert Config().max_session_s == 7200.0


class TestMeetingId:
    def test_first_part_names_the_meeting_after_itself(self, tmp_path):
        outbox = Outbox(tmp_path)
        path = outbox.start("s-1", "2026-08-26T10:00:00+07:00", app=None,
                            window_title=None, device_hint=None)
        outbox.append(path, 1.0, "mic", "начали")
        payload = read_payload(path)
        assert payload["meeting_id"] == "s-1"
        assert payload["part"] == 1

    def test_continuation_carries_the_same_meeting_id(self, tmp_path):
        outbox = Outbox(tmp_path)
        first = outbox.start("s-1", "2026-08-26T10:00:00+07:00", app=None,
                             window_title=None, device_hint=None)
        outbox.append(first, 1.0, "mic", "первая половина")
        second = outbox.start("s-2", "2026-08-26T12:00:00+07:00", app=None,
                              window_title=None, device_hint=None,
                              meeting_id="s-1", part=2)
        outbox.append(second, 1.0, "mic", "вторая половина")

        head, tail = read_payload(first), read_payload(second)
        assert head["meeting_id"] == tail["meeting_id"] == "s-1"
        assert (head["part"], tail["part"]) == (1, 2)

    def test_old_queue_file_without_the_field_still_reads(self, tmp_path):
        """В очереди могут лежать файлы, записанные до появления meeting_id."""
        path = tmp_path / "s-old.jsonl"
        path.write_text(
            '{"kind": "header", "started_at": "2026-08-20T10:00:00+07:00"}\n'
            '{"kind": "u", "at": 1.0, "stream": "mic", "text": "старая сессия"}\n',
            encoding="utf-8")
        payload = read_payload(path)
        assert payload["meeting_id"] == "s-old"
        assert payload["part"] == 1
