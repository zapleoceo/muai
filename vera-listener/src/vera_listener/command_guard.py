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
#: Системная дорожка не пишется (кадров нет) — не проверить, чей голос.
BLIND = "blind"

#: Соседние кадры и куски стыкуются в плавающей точке не ровно: без допуска
#: сплошная речь рассыпалась бы на тысячи отрезков по 30 мс.
_EPS = 0.05

#: Кадры двух дорожек приходят в общую очередь не строго вперемешку: system
#: может отставать от mic на доли секунды. Решаем, когда часы ушли дальше окна
#: с запасом, — иначе хвост окна выглядел бы «без кадров» из-за очерёдности.
_LAG_S = 1.0

#: Разрыв в кадрах system, с которого дорожка считается НЕ пишущейся, а не
#: дрожащей. Кадр — 32 мс, и паузы в десятки–сотни миллисекунд на захвате
#: обычны (планировщик Windows, GC, всплеск распознавания в соседнем потоке);
#: при допуске 50 мс ревью получило отказ законной команды на паузах 0.06 и
#: 0.3 с. Мёртвое устройство выглядит иначе: захват ждёт `REOPEN_PAUSE_S` = 5 с
#: перед переоткрытием, то есть разрыв не короче 5 с. Секунда — впятеро ниже
#: этого и втрое выше худшей замеченной паузы джиттера.
BLIND_GAP_S = 1.0


@dataclass
class Spans:
    """Отрезки времени сессии: где звучала речь или что уже распознано."""

    items: list[tuple[float, float]] = field(default_factory=list)
    #: Разрыв короче этого склеивается: это тот же сплошной отрезок.
    gap: float = _EPS

    def add(self, start: float, end: float) -> None:
        if self.items and start <= self.items[-1][1] + self.gap:
            last_start, last_end = self.items[-1]
            self.items[-1] = (last_start, max(last_end, end))
            return
        self.items.append((start, end))

    def overlapping(self, start: float, end: float) -> list[tuple[float, float]]:
        return [(a, b) for a, b in self.items if a < end and b > start]

    def covers(self, start: float, end: float) -> bool:
        return any(a <= start + self.gap and b >= end - self.gap
                   for a, b in self.items)

    @property
    def first(self) -> float | None:
        return self.items[0][0] if self.items else None


class SystemTrack:
    """Что известно о системном звуке сессии. Пишут два потока — отсюда замок.

    Захват (главный поток) отмечает речь по VAD и сколько уже услышано,
    распознавание — готовые реплики и какие отрезки распознаны.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._speech = Spans()
        # Где кадры системной дорожки РЕАЛЬНО приходили. «Loopback молчал» и
        # «loopback нет» — разные вещи: устройство отвалилось, захват ждёт
        # переоткрытия, кадров нет — и без этого учёта пустота выглядела бы
        # тишиной, а голос из динамиков в микрофоне прошёл бы как владелец.
        self._frames = Spans(gap=BLIND_GAP_S)
        self._transcribed: list[tuple[float, float]] = []
        self._utterances: list[tuple[float, str]] = []
        self.heard_until = 0.0

    def hear(self, at: float, duration: float, speech: bool, *,
             system: bool = True) -> None:
        """`system=False` — кадр микрофона: двигает только часы."""
        with self._lock:
            if system:
                self._frames.add(at, at + duration)
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
        if not final and self.heard_until < end + VAD_SLACK_S + _LAG_S:
            return WAIT
        first = self._frames.first
        if first is None:
            return BLIND
        # System появился уже ПОСЛЕ начала фразы — пока она звучала, динамиков
        # никто не слушал, и доказать, что они молчали, нечем. Без этой
        # проверки окно «от первого кадра» сжималось бы в пустоту, а пустое
        # окно `covers` считает покрытым — и выходило бы OWN.
        if first > at:
            return BLIND
        # Окно не раньше первого кадра system: захват двух дорожек стартует не
        # одновременно, и без этого команда в первые секунды сессии казалась
        # бы сказанной «вслепую».
        lo_vad = max(at - VAD_SLACK_S, first)
        hi_vad = end + VAD_SLACK_S
        if final:
            hi_vad = min(hi_vad, self.heard_until)
        if not self._frames.covers(lo_vad, hi_vad):
            # Системной дорожки на части окна не было — доказать, что
            # динамики молчали, нечем. Ждать нечего тоже: пропавшие кадры не
            # вернутся, а на закрытии это отказ.
            return BLIND
        if not self._speech.overlapping(lo_vad, hi_vad):
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

    def speakers_silent(self, at: float, end: float) -> bool:
        """Молчали ли динамики в окне VAD вокруг реплики — быстрый путь OWN.

        Для уверенности (`command_intake.confidence`): OWN по молчанию
        динамиков надёжнее, чем OWN по сверке текста с системным звуком.
        """
        with self._lock:
            first = self._frames.first or 0.0
            return not self._speech.overlapping(max(at - VAD_SLACK_S, first),
                                                end + VAD_SLACK_S)

    def _all_transcribed(self, lo: float, hi: float) -> bool:
        spans = Spans()
        for start, end in sorted(self._transcribed):
            spans.add(start, end)
        # Речь, начавшаяся до окна, распознана куском, который тоже начался
        # раньше, — поэтому проверяем покрытие самого отрезка речи, а не окна.
        return all(spans.covers(a, b) for a, b in self._speech.overlapping(lo, hi))
