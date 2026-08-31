"""Лог предохранителя не путается с обычным закрытием сессии.

`_queue_chunk` зовётся из двух мест: `_record()` — после `ready()==True`
(обычный ход или срабатывание предохранителя), и `_finish()` — на закрытии
сессии флашит ХВОСТ независимо от `ready()`. У хвоста `silence_s` почти
всегда мал, и без разведения этих путей предохранитель ложно засчитывал бы
себя на КАЖДОМ закрытии сессии с недомолчанным остатком речи. Поймано на
этом же шаге, до коммита — не гипотетический риск, реальный баг в первой
версии правки.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from vera_listener.app import Listener
from vera_listener.capture import MIC
from vera_listener.config import Config
from vera_listener.recorder import PAUSE_FLUSH_S
from vera_listener.segmenter import Closed, Session
from vera_listener.vad import FRAME_S


def _listener(tmp_path, **over) -> Listener:
    config = replace(Config(root=tmp_path, internal_secret="x"), **over)
    listener = Listener(config)
    listener.segmenter.feed(0.0, MIC, True, app="zoom.exe", window_title="окно")
    listener._ensure_open()
    return listener


class TestForcedFlushLog:
    """Прямой вызов через `_record`, а не через `Capture`/потоки: это
    юнит-тест конкретной логики, не интеграционный тест захвата звука."""

    def test_fires_when_ceiling_reached_without_pause(self, tmp_path, caplog):
        listener = _listener(tmp_path, chunk_speech_s=1.0, chunk_max_wall_s=1.0)
        caplog.set_level(logging.INFO, logger="listener")
        recorder = listener.recorders[MIC]
        at = 0.0
        n = int(1.5 / FRAME_S) + 1
        for _ in range(n):
            recorder.add(b"x", True, at)
            if recorder.ready():
                listener._queue_chunk(MIC, via_ready=True)
                break
            at += FRAME_S
        assert any("предохранителю" in r.message for r in caplog.records)

    def test_does_not_fire_on_normal_pause_flush(self, tmp_path, caplog):
        listener = _listener(tmp_path, chunk_speech_s=1.0, chunk_max_wall_s=300.0)
        caplog.set_level(logging.INFO, logger="listener")
        recorder = listener.recorders[MIC]
        at = 0.0
        for _ in range(int(1.5 / FRAME_S) + 1):
            recorder.add(b"x", True, at)
            at += FRAME_S
        for _ in range(int(PAUSE_FLUSH_S / FRAME_S) + 2):
            recorder.add(b"x", False, at)
            at += FRAME_S
        assert recorder.ready()
        listener._queue_chunk(MIC, via_ready=True)
        assert not any("предохранителю" in r.message for r in caplog.records)

    def test_session_close_never_logs_it_even_with_low_silence(self, tmp_path, caplog):
        """Хвост речи без паузы, закрытый через `_finish` (не `ready()`):
        не должен звучать так, будто сработал предохранитель."""
        listener = _listener(tmp_path, chunk_speech_s=60.0, chunk_max_wall_s=300.0)
        caplog.set_level(logging.INFO, logger="listener")
        recorder = listener.recorders[MIC]
        at = 0.0
        # Меньше нормы речи и предохранителя — ready() тут в принципе False.
        for _ in range(int(3.0 / FRAME_S) + 1):
            recorder.add(b"x", True, at)
            at += FRAME_S
        assert not recorder.ready()

        listener._finish(Closed(
            session=Session(started_at=0.0, app="zoom.exe", window_title="окно",
                            last_speech_at=at, speech_s={MIC: 3.0}),
            ended_at=at, reason="silence"))
        assert not any("предохранителю" in r.message for r in caplog.records)
