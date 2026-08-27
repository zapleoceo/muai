"""Куда уходит системный звук: в распознавание или в копилку.

Главный регресс, который эти тесты держат: ролик в браузере НЕ должен попадать
в whisper. 2026-08-27 слушатель прогнал 257 минут аудио и выбросил 22 сессии из
27 — ворота считались только при закрытии, то есть после того, как за ролик уже
заплатили полутора ядрами.
"""
from __future__ import annotations

from dataclasses import replace

from vera_listener.app import Listener
from vera_listener.capture import MIC, SYSTEM
from vera_listener.config import Config
from vera_listener.hold import BYTES_PER_S


def _listener(tmp_path, **over) -> Listener:
    config = replace(Config(root=tmp_path, internal_secret="x"), **over)
    return Listener(config)


def _pcm(seconds: float) -> bytes:
    return b"\x00" * int(seconds * BYTES_PER_S)


def _open_session(listener: Listener, *, app: str | None, mic_speech_s: float = 0.0):
    """Открыть сессию как это делает насос кадров, без звука и потоков."""
    listener.segmenter.feed(0.0, MIC, True, app=app, window_title="окно")
    listener.segmenter.current.speech_s[MIC] = mic_speech_s
    listener._ensure_open()


def _queue(listener: Listener) -> list[tuple]:
    return list(listener.jobs.queue)


class TestRouting:
    def test_video_in_browser_never_reaches_whisper(self, tmp_path):
        """Ютуб: микрофон молчит, значит системный звук — в копилку."""
        listener = _listener(tmp_path)
        _open_session(listener, app="chrome.exe", mic_speech_s=0.0)
        listener.recorders[SYSTEM].add(_pcm(3), True, 0.0)
        listener._queue_chunk(SYSTEM)
        assert _queue(listener) == []
        assert listener._held.seconds == 3.0

    def test_zoom_is_transcribed_immediately(self, tmp_path):
        """Разрешённое приложение сомнений не вызывает — незачем ждать."""
        listener = _listener(tmp_path)
        _open_session(listener, app="zoom.exe", mic_speech_s=0.0)
        listener.recorders[SYSTEM].add(_pcm(3), True, 0.0)
        listener._queue_chunk(SYSTEM)
        assert [job[0] for job in _queue(listener)] == ["chunk"]
        assert listener._held.seconds == 0.0

    def test_microphone_is_always_transcribed(self, tmp_path):
        """Голос владельца ценен сам по себе и придерживать его нечего."""
        listener = _listener(tmp_path)
        _open_session(listener, app="chrome.exe", mic_speech_s=0.0)
        listener.recorders[MIC].add(_pcm(3), True, 0.0)
        listener._queue_chunk(MIC)
        assert [job[2] for job in _queue(listener)] == [MIC]

    def test_unknown_app_holds_too(self, tmp_path):
        """Окно «Claude» и Total Commander принимались за разговор 11 раз за сутки."""
        listener = _listener(tmp_path)
        _open_session(listener, app=None, mic_speech_s=0.0)
        listener.recorders[SYSTEM].add(_pcm(2), True, 0.0)
        listener._queue_chunk(SYSTEM)
        assert _queue(listener) == []
        assert listener._held.seconds == 2.0

    def test_denied_app_holds_even_with_speech(self, tmp_path):
        listener = _listener(tmp_path, deny_apps=("spotify.exe",))
        _open_session(listener, app="spotify.exe", mic_speech_s=60.0)
        listener.recorders[SYSTEM].add(_pcm(2), True, 0.0)
        listener._queue_chunk(SYSTEM)
        assert _queue(listener) == []


class TestFlush:
    def test_speaking_up_releases_the_hold(self, tmp_path):
        """Созвон в Meet: пока слушал — придержали, заговорил — уехало целиком."""
        listener = _listener(tmp_path)
        _open_session(listener, app="chrome.exe", mic_speech_s=0.0)
        listener.recorders[SYSTEM].add(_pcm(2), True, 0.0)
        listener._queue_chunk(SYSTEM)
        assert listener._held.seconds == 2.0

        listener.segmenter.current.speech_s[MIC] = 10.0
        listener._flush_held()
        jobs = _queue(listener)
        assert [job[2] for job in jobs] == [SYSTEM]
        assert listener._held.seconds == 0.0

    def test_nothing_is_lost_when_the_hold_releases(self, tmp_path):
        """Смещения сохраняются — иначе реплики съедут по времени."""
        listener = _listener(tmp_path)
        _open_session(listener, app="chrome.exe")
        listener.recorders[SYSTEM].add(_pcm(1), True, 0.0)
        listener._queue_chunk(SYSTEM)
        listener.recorders[SYSTEM].add(_pcm(1), True, 30.0)
        listener._queue_chunk(SYSTEM)

        listener.segmenter.current.speech_s[MIC] = 10.0
        listener._flush_held()
        assert [job[3] for job in _queue(listener)] == [0.0, 30.0]


class TestClose:
    def test_close_carries_the_hold_to_the_verdict(self, tmp_path):
        """Решает вердикт: примут сессию — придержанное распознают, нет — выбросят."""
        listener = _listener(tmp_path)
        _open_session(listener, app="chrome.exe")
        listener.recorders[SYSTEM].add(_pcm(2), True, 0.0)
        listener._queue_chunk(SYSTEM)

        closed = listener.segmenter.flush(120.0, reason="silence")
        listener._finish(closed)
        close_jobs = [job for job in _queue(listener) if job[0] == "close"]
        assert len(close_jobs) == 1
        held = close_jobs[0][5]
        assert [offset for offset, _pcm_ in held] == [0.0]
        assert listener._held.seconds == 0.0
