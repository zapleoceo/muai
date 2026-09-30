"""Чей это голос: владельца или собеседника из динамиков, попавшего в микрофон.

Команда Вере — это действие, поэтому отбор здесь строже, чем для стенограммы.
Эхо в стенограмме помечается на закрытии сессии (`dedup.mark_echo`), а команде
ждать закрытия нельзя. На лету решаем в две ступени:

1. Физика. Эхо — это звук из динамиков, а всё, что играет в динамиках, слышит
   loopback-дорожка. Если в окне вокруг реплики loopback молчал (VAD, без
   распознавания), говорить из динамиков было некому: голос владельца.
   Это быстрый путь — именно он даёт ответ без задержки.
2. Текст. Loopback звучал — сравниваем реплику с распознанным системным звуком
   тем же `looks_like_echo`, что и стенограмма, и в том же окне. Для этого
   нужно, чтобы системный звук вокруг реплики уже был распознан; пока нет —
   решение откладывается («wait»), а не принимается наугад.

Непроверяемое не исполняется: сессию закрыли, а системный звук вокруг реплики
так и не распознан (ворота его не пропустили) — команда отбрасывается.
Пропустить команду владельца дешевле, чем исполнить чужую.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from vera_listener.dedup import LONG_WINDOW_S, looks_like_echo, window_for

#: Запас для ступени VAD. Время реплики берётся из разметки whisper внутри
#: куска и плывёт на секунды, поэтому окно шире самой реплики. Взято равным
#: `dedup.WINDOW_S` — тому разбегу, что намерен на живой записи 31.08, — а не
#: подобрано отдельно: живых замеров команды ещё нет.
VAD_SLACK_S = 6.0

OWN, ECHO, WAIT = "own", "echo", "wait"

#: Соседние кадры и куски стыкуются в плавающей точке не ровно: без допуска
#: сплошная речь рассыпалась бы на тысячи отрезков по 30 мс.
_EPS = 0.05


@dataclass
class Spans:
    """Отрезки времени сессии: где звучала речь или что уже распознано."""

    items: list[tuple[float, float]] = field(default_factory=list)

    def add(self, start: float, end: float) -> None:
        if self.items and start <= self.items[-1][1] + _EPS:
            last_start, last_end = self.items[-1]
            self.items[-1] = (last_start, max(last_end, end))
            return
        self.items.append((start, end))

    def overlapping(self, start: float, end: float) -> list[tuple[float, float]]:
        return [(a, b) for a, b in self.items if a < end and b > start]

    def covers(self, start: float, end: float) -> bool:
        return any(a <= start + _EPS and b >= end - _EPS for a, b in self.items)


class SystemTrack:
    """Что известно о системном звуке сессии. Пишут два потока — отсюда замок.

    Захват (главный поток) отмечает речь по VAD и сколько уже услышано,
    распознавание — готовые реплики и какие отрезки распознаны.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._speech = Spans()
        self._transcribed: list[tuple[float, float]] = []
        self._utterances: list[tuple[float, str]] = []
        self.heard_until = 0.0

    def hear(self, at: float, duration: float, speech: bool) -> None:
        with self._lock:
            if speech:
                self._speech.add(at, at + duration)
            self.heard_until = max(self.heard_until, at + duration)

    def transcribed(self, start: float, end: float,
                    utterances: list[tuple[float, str]]) -> None:
        with self._lock:
            self._transcribed.append((start, end))
            self._utterances.extend(utterances)

    def verdict(self, at: float, end: float, text: str, *, final: bool) -> str:
        with self._lock:
            return self._verdict(at, end, text, final=final)

    def _verdict(self, at: float, end: float, text: str, *, final: bool) -> str:
        if not final and self.heard_until < end + VAD_SLACK_S:
            return WAIT
        if not self._speech.overlapping(at - VAD_SLACK_S, end + VAD_SLACK_S):
            return OWN
        lo, hi = at - LONG_WINDOW_S, end + LONG_WINDOW_S
        if not final and self.heard_until < hi:
            return WAIT
        if not self._all_transcribed(lo, hi):
            # На закрытии досказать уже нечего: не распознанный к этому
            # моменту звук не распознается никогда.
            return ECHO if final else WAIT
        for other_at, other_text in self._utterances:
            if (abs(other_at - at) <= window_for(text, other_text)
                    and looks_like_echo(text, other_text)):
                return ECHO
        return OWN

    def _all_transcribed(self, lo: float, hi: float) -> bool:
        spans = Spans()
        for start, end in sorted(self._transcribed):
            spans.add(start, end)
        # Речь, начавшаяся до окна, распознана куском, который тоже начался
        # раньше, — поэтому проверяем покрытие самого отрезка речи, а не окна.
        return all(spans.covers(a, b) for a, b in self._speech.overlapping(lo, hi))
