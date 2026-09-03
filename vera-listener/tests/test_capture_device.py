"""Захват переживает смену устройства по умолчанию.

Дефект, ради которого эти тесты написаны, был тихим: устройство выбиралось
один раз при открытии дорожки, а дальше цикл писал вечно. Воткнули наушники —
старые колонки живы, запись идёт, ошибки нет, но в них уже ничего не играет.
Дорожка `system` превращалась в тишину, и собеседник исчезал из разговора
целиком, не оставив ни строки в логе.

Поэтому проверяется не «устройство можно открыть», а именно ПЕРЕОТКРЫТИЕ:
за какое имя слушатель держится после переключения.
"""
from __future__ import annotations

import queue

import numpy as np
import pytest

from vera_listener import capture as cap

#: Предохранитель. `_run` ловит `Exception` и уходит на новый круг, поэтому
#: ошибка в подставке дала бы не падение теста, а вечную петлю переоткрытий —
#: причём счётчик кадров её не поймает: до `record()` дело просто не дойдёт.
#: Поэтому считаем ЛЮБОЕ обращение к подставке и рвём через `BaseException`,
#: мимо `except Exception` в захвате.
MAX_CALLS = 200


class Abort(BaseException):
    """Обрыв теста мимо `except Exception` — иначе он не оборвётся."""


class FakeRecorder:
    def __init__(self, sound):
        self._sound = sound

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes):
        self._sound.note()
        self._sound.records += 1
        self._sound.on_record()
        return np.zeros((numframes, 1), dtype=np.float32)


class FakeDevice:
    def __init__(self, name, sound):
        self.name = name
        self._sound = sound

    def recorder(self, samplerate, channels, blocksize):
        self._sound.note()
        self._sound.opened.append(self.name)
        return FakeRecorder(self._sound)


class FakeSound:
    """Ровно та часть `soundcard`, которой пользуется захват."""

    def __init__(self, name: str):
        self.default = name
        self.opened: list[str] = []
        self.records = 0
        self.calls = 0
        self.on_record = lambda: None

    def note(self) -> None:
        self.calls += 1
        if self.calls > MAX_CALLS:
            raise Abort(f"захват не остановился за {MAX_CALLS} обращений")

    def default_speaker(self):
        self.note()
        return FakeDevice(self.default, self)

    def default_microphone(self):
        self.note()
        return FakeDevice(self.default, self)

    def get_microphone(self, id, include_loopback):
        self.note()
        return FakeDevice(id, self)


@pytest.fixture
def sound(monkeypatch):
    fake = FakeSound("Speakers (Realtek)")
    monkeypatch.setattr(cap, "sc", fake)
    # Проверка «не сменилось ли устройство» привязана к стенным секундам;
    # обнуляем шаг, чтобы тест не ждал их по-настоящему.
    monkeypatch.setattr(cap, "DEVICE_CHECK_S", 0.0)
    monkeypatch.setattr(cap, "REOPEN_PAUSE_S", 0.0)
    return fake


def _drive(sound, capture, *, switch_at: int | None, switch_to: str, stop_at: int):
    def on_record():
        if switch_at is not None and sound.records == switch_at:
            sound.default = switch_to
        if sound.records >= stop_at:
            capture._stop.set()

    sound.on_record = on_record


class TestRebinding:
    def test_system_track_follows_the_output_switch(self, sound):
        """Наушники стали устройством по умолчанию — пишем петлю наушников."""
        frames: queue.Queue = queue.Queue()
        capture = cap.Capture(frames)
        _drive(sound, capture, switch_at=3, switch_to="Headphones (soundcore)",
               stop_at=6)

        capture._run(cap.SYSTEM)

        assert sound.opened == ["Speakers (Realtek)", "Headphones (soundcore)"]
        assert capture.device_hint == "Headphones (soundcore)"

    def test_mic_track_follows_the_input_switch(self, sound):
        """Гарнитура приносит и свой микрофон — дорожка mic тоже переезжает."""
        capture = cap.Capture(queue.Queue())
        _drive(sound, capture, switch_at=2, switch_to="Headset Mic", stop_at=5)

        capture._run(cap.MIC)

        assert sound.opened == ["Speakers (Realtek)", "Headset Mic"]

    def test_steady_device_is_opened_once(self, sound):
        """Устройство не менялось — лишних переоткрытий быть не должно."""
        capture = cap.Capture(queue.Queue())
        _drive(sound, capture, switch_at=None, switch_to="", stop_at=8)

        capture._run(cap.SYSTEM)

        assert sound.opened == ["Speakers (Realtek)"]

    def test_frames_keep_flowing_across_the_switch(self, sound):
        """Переоткрытие не должно съедать кадры: разрыв в записи — потеря речи."""
        frames: queue.Queue = queue.Queue()
        capture = cap.Capture(frames)
        _drive(sound, capture, switch_at=3, switch_to="Headphones", stop_at=6)

        capture._run(cap.SYSTEM)

        assert frames.qsize() == sound.records
        assert {f.track for f in frames.queue} == {cap.SYSTEM}
