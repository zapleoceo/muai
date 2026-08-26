"""Живое состояние слушателя — одно место, откуда его читает иконка в трее.

Слушатель пишет сюда по ходу работы, трей читает раз в секунду. Через
блокировку, потому что пишут три потока (нарезка, распознавание, отправка),
а читает четвёртый.

Держим ровно то, что нужно ответить на вопрос «оно вообще работает?»:
слышит ли микрофон, идёт ли сейчас разговор, сколько лежит неотправленного и
когда последний раз что-то ушло в мозг.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: слушатель жив, но ничего не происходит — тишина
IDLE = "idle"
#: идёт разговор, дорожки пишутся
TALKING = "talking"
#: устройства недоступны (чужая сессия Windows, наушники выдернули)
DEAF = "deaf"
#: сеть недоступна, очередь копится
OFFLINE = "offline"


@dataclass
class Status:
    state: str = IDLE
    #: сколько разговоров ждёт отправки
    queued: int = 0
    #: сколько ушло в мозг с момента запуска
    sent: int = 0
    #: сколько отброшено отсевом (не разговор)
    dropped: int = 0
    started_at: float = field(default_factory=time.monotonic)
    last_sent_at: float | None = None
    last_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    def note_sent(self, count: int, queued: int) -> None:
        with self._lock:
            self.sent += count
            self.queued = queued
            self.last_error = None
            if count:
                self.last_sent_at = time.monotonic()
            self.state = OFFLINE if queued else IDLE

    def note_dropped(self) -> None:
        with self._lock:
            self.dropped += 1

    def note_error(self, text: str) -> None:
        with self._lock:
            self.last_error = text[:200]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state, "queued": self.queued, "sent": self.sent,
                "dropped": self.dropped, "last_error": self.last_error,
                "uptime_s": time.monotonic() - self.started_at,
                "since_sent_s": (None if self.last_sent_at is None
                                 else time.monotonic() - self.last_sent_at),
            }
