"""Что системная дорожка слышала до открытия сессии — для проверки голоса.

Сессия открывается по первой речи, и `CommandWatch` рождается вместе с ней:
кадры loopback, пришедшие раньше, слежение не видело. Кодовая фраза, открывшая
сессию (смещение 0.0 с), выходила поэтому «вслепую» — первый кадр system
оказывался позже начала фразы, и `command_guard` честно отвечал BLIND.

Пропустить проверку первых секунд фразы нельзя: короткое эхо из динамиков в
этом окне стало бы голосом владельца. Вместо этого держим скользящий хвост
наблюдений system — ровно то, что guard берёт из кадра (время, длительность,
флаг VAD), без PCM — и при открытии сессии проигрываем его в новое слежение.
Чего в хвосте нет, того нет: покрытие не дотянулось до начала фразы — BLIND
остаётся (fail closed).

Распознанного текста хвост не несёт: речь в динамиках перед фразой guard
сверяет по тексту, а его для кадров до сессии нет. На закрытии такое поручение
уходит в ECHO — отброс, а не владелец.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable

from vera_listener.command_guard import LAG_S, VAD_SLACK_S

#: Запас сверх окна guard на джиттер кадров у самого края хвоста.
PREROLL_MARGIN_S = 1.0
#: Глубже всего guard смотрит назад от начала фразы на `VAD_SLACK_S`; фраза
#: на 0.0 с сессии требует покрытия с −VAD_SLACK_S. Сверху — разбег очереди
#: двух дорожек (`LAG_S`) и запас: 6 + 1 + 1 = 8 с.
PREROLL_S = VAD_SLACK_S + LAG_S + PREROLL_MARGIN_S
#: Жёсткий потолок памяти на случай кадров с дробным шагом: кадр 32 мс →
#: в 8 с их 250, потолок вчетверо выше.
MAX_FRAMES = 1024


class PreRoll:
    def __init__(self, keep_s: float = PREROLL_S, max_frames: int = MAX_FRAMES):
        self.keep_s = keep_s
        self._frames: deque[tuple[float, float, bool]] = deque(maxlen=max_frames)

    def __len__(self) -> int:
        return len(self._frames)

    def observe(self, at: float, duration: float, speech: bool) -> None:
        self._frames.append((at, duration, speech))
        horizon = at + duration - self.keep_s
        while self._frames and self._frames[0][0] + self._frames[0][1] < horizon:
            self._frames.popleft()

    def replay(self, hear: Callable[[float, float, bool], None], zero: float) -> int:
        """Отдать хвост в слежение новой сессии во времени сессии. → сколько."""
        for at, duration, speech in self._frames:
            hear(at - zero, duration, speech)
        return len(self._frames)
