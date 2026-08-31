"""Накопитель куска: обычный флаш по паузе и предохранитель по времени.

Без предохранителя `ready()` требует И норму речи, И паузу ≥2с — а пауза
может не случиться минутами на быстром разговоре или монологе. Замер
(2026-08-31): 11 минут почти непрерывной речи (паузы по 0.4с) не дают ни
одного срабатывания, буфер растёт без предела.
"""
from __future__ import annotations

import math

from vera_listener.recorder import PAUSE_FLUSH_S, TrackRecorder
from vera_listener.vad import FRAME_S


def _speak(r: TrackRecorder, at: float, seconds: float) -> float:
    """Кормит кадрами речи, возвращает `at` после них.

    `ceil`, а не `round`: FRAME_S=0.032 не делит секунды ровно, и `round` на
    границе (например 2.0с → 62.5 кадра) уходит вниз по банковскому
    округлению — запрошенные 2.0с реально набирались бы как 1.984, ниже
    PAUSE_FLUSH_S. `ceil` гарантирует минимум запрошенное время.
    """
    n = math.ceil(seconds / FRAME_S)
    for _ in range(n):
        r.add(b"x", True, at)
        at += FRAME_S
    return at


def _silence(r: TrackRecorder, at: float, seconds: float) -> float:
    n = math.ceil(seconds / FRAME_S)
    for _ in range(n):
        r.add(b"x", False, at)
        at += FRAME_S
    return at


class TestNormalFlushByPause:
    def test_not_ready_before_speech_norm(self):
        r = TrackRecorder("mic", chunk_speech_s=10.0)
        _speak(r, 0.0, 5.0)
        assert not r.ready()

    def test_ready_after_norm_and_pause(self):
        r = TrackRecorder("mic", chunk_speech_s=10.0)
        at = _speak(r, 0.0, 10.0)
        at = _silence(r, at, PAUSE_FLUSH_S)
        assert r.ready()

    def test_not_ready_with_norm_but_short_pause(self):
        """Норма набрана, но пауза короче 2с — обычное условие не срабатывает."""
        r = TrackRecorder("mic", chunk_speech_s=10.0)
        at = _speak(r, 0.0, 10.0)
        at = _silence(r, at, PAUSE_FLUSH_S - 0.2)
        assert not r.ready()

    def test_speech_resets_silence_counter(self):
        r = TrackRecorder("mic", chunk_speech_s=10.0)
        at = _speak(r, 0.0, 10.0)
        at = _silence(r, at, PAUSE_FLUSH_S - 0.2)
        at = _speak(r, at, 0.1)
        assert not r.ready()


class TestWallClockSafety:
    """Предохранитель: без паузы кусок всё равно закрывается по времени."""

    def test_never_ready_without_the_safety_net(self):
        """Контрольный замер: то же самое без предохранителя (искусственно
        завышенным потолком) НЕ срабатывает — доказывает, что дальнейшие
        тесты проверяют именно предохранитель, а не побочный эффект."""
        r = TrackRecorder("mic", chunk_speech_s=60.0, max_wall_s=10_000.0)
        at = 0.0
        for _ in range(50):
            at = _speak(r, at, 3.0)
            at = _silence(r, at, PAUSE_FLUSH_S - 0.5)  # короче порога всегда
        assert not r.ready()
        assert at > 175.0  # почти 3 минуты модельного времени, реально прогнано

    def test_fires_once_wall_clock_elapsed(self):
        r = TrackRecorder("mic", chunk_speech_s=60.0, max_wall_s=60.0)
        at = 0.0
        fired_at = None
        for _ in range(50):
            at = _speak(r, at, 3.0)
            at = _silence(r, at, PAUSE_FLUSH_S - 0.5)
            if r.ready():
                fired_at = at
                break
        assert fired_at is not None
        assert fired_at >= 60.0

    def test_does_not_fire_before_the_ceiling(self):
        r = TrackRecorder("mic", chunk_speech_s=60.0, max_wall_s=60.0)
        _speak(r, 0.0, 30.0)
        assert not r.ready()

    def test_take_returns_everything_accumulated_so_far(self):
        """Принудительный флаш не должен ронять данные — иначе предохранитель
        сам стал бы источником потерь, которые должен предотвращать."""
        r = TrackRecorder("mic", chunk_speech_s=60.0, max_wall_s=5.0)
        _speak(r, 0.0, 6.0)
        assert r.ready()
        offset, pcm = r.take()
        assert offset == 0.0
        assert len(pcm) > 0

    def test_default_ceiling_is_five_minutes(self):
        assert TrackRecorder("mic", chunk_speech_s=60.0).max_wall_s == 300.0


class TestSilenceProperty:
    def test_exposes_current_silence_run(self):
        r = TrackRecorder("mic", chunk_speech_s=60.0)
        at = _speak(r, 0.0, 1.0)
        _silence(r, at, 0.5)
        assert 0.4 < r.silence_s < 0.6

    def test_zero_before_any_frame(self):
        assert TrackRecorder("mic", chunk_speech_s=60.0).silence_s == 0.0
