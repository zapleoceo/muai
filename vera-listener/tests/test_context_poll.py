"""Опрос «кто сейчас звучит» — только когда ответ кому-то нужен.

Перебор звуковых сессий Windows через pycaw стоит 28 мс процессора на вызов
(замер 04.09). Раз в две секунды это 1.4% ядра НЕПРЕРЫВНО — десятая часть
всего расхода слушателя, и почти всё это в тишине, когда ответ не читает
никто: контекст нужен сегментатору, а тот спрашивает его с первой речевой
рамки.

Тесты держат обе стороны размена: экономию в тишине и свежесть контекста в тот
момент, когда он впервые понадобился. Вторая половина важнее: сэкономить, но
открыть разговор с пустым приложением — значит сломать отсев роликов.
"""
from __future__ import annotations

import pytest

from vera_listener import app as app_module
from vera_listener.app import Listener
from vera_listener.capture import MIC, Frame
from vera_listener.config import Config


class Spy:
    """Считает, сколько раз спросили про звучащее приложение."""

    def __init__(self, name: str = "chrome.exe"):
        self.name = name
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        return self.name


@pytest.fixture
def spy(monkeypatch) -> Spy:
    probe = Spy()
    monkeypatch.setattr(app_module, "active_audio_app", probe)
    monkeypatch.setattr(app_module, "foreground_window_title", lambda: "окно")
    return probe


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """ОСТОРОЖНО: подменяет `time.monotonic` у самого модуля `time`, то есть на
    весь процесс. Здесь это безопасно — тесты зовут `_pump` напрямую, фоновые
    потоки слушателя не запущены. Понадобится тест, который поднимает потоки,
    — часы надо будет сузить, иначе гонка."""
    fake = Clock()
    monkeypatch.setattr(app_module.time, "monotonic", fake.monotonic)
    return fake


def _listener(tmp_path) -> Listener:
    return Listener(Config(root=tmp_path, internal_secret="x"))


class Clock:
    """Поддельные часы: секунда за кадр.

    Без них тест почти ничего не доказывает: настоящий цикл пролетает за
    миллисекунды, порог в две секунды не наступает ни разу, и старый код
    успевал опросить всего однажды. С часами тридцать кадров тишины — это
    тридцать секунд, то есть пятнадцать опросов у старого кода против нуля
    у нового.
    """

    def __init__(self):
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def tick(self) -> None:
        self.now += 1.0


def _pump_frames(listener: Listener, speech: list[bool], clock: Clock) -> None:
    """Прогнать через насос кадры с заданной речевой разметкой.

    Гоняем настоящий `_pump`, а не его пересказ: экономия живёт именно в
    порядке условий внутри цикла, и тест мимо него ничего бы не доказал.
    """
    listener._stop.clear()
    marks = iter(speech)

    def is_speech(_pcm: bytes) -> bool:
        clock.tick()
        try:
            return next(marks)
        except StopIteration:
            listener._stop.set()
            return False

    listener.detectors[MIC].is_speech = is_speech
    for i in range(len(speech) + 1):
        listener.frames.put(Frame(MIC, float(i), bytes(1024)))
    listener._pump()


class TestLazyContextPoll:
    def test_silence_does_not_ask_who_is_sounding(self, tmp_path, spy, clock):
        """Тридцать секунд тишины: ни одного опроса (у старого кода — 15)."""
        listener = _listener(tmp_path)
        _pump_frames(listener, [False] * 30, clock)
        assert spy.calls == 0

    def test_first_speech_frame_asks_immediately(self, tmp_path, spy, clock):
        """Речь пошла — контекст обязан быть свежим на ТОЙ ЖЕ рамке.

        Иначе сессия откроется с пустым приложением, и отсев роликов
        (media_only) не сработает — экономия ценой дефекта.
        """
        listener = _listener(tmp_path)
        _pump_frames(listener, [False] * 10 + [True], clock)
        assert spy.calls == 1
        assert listener.segmenter.current is not None
        assert listener.segmenter.current.app == "chrome.exe"

    def test_open_session_keeps_polling_through_pauses(self, tmp_path, spy, clock):
        """Внутри разговора паузы обычны, а приложение может смениться.

        Пока сессия открыта, опрос идёт по расписанию независимо от речи —
        иначе смена приложения посреди созвона осталась бы незамеченной.
        """
        listener = _listener(tmp_path)
        _pump_frames(listener, [True] + [False] * 5, clock)
        assert listener.segmenter.current is not None
        # Пять секунд паузы при пороге в две — опрос обязан был повториться.
        assert spy.calls >= 2
