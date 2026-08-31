"""Накопитель аудио одной дорожки между отправками в распознавание.

Тишина до первой речи не копится, хвост тишины обрезается: держать в памяти
паузы незачем, а распознавание на них только тратит процессор.
"""
from __future__ import annotations

from vera_listener.vad import FRAME_S

PAUSE_FLUSH_S = 2.0

#: Предохранитель: без паузы ≥2с кусок не флашится вообще (условие `ready()`
#: требует и норму речи, И паузу). На быстром разговоре или монологе без
#: остановок пауза может не случиться минутами — проверено: 11 минут почти
#: непрерывной речи (паузы по 0.4с, короче PAUSE_FLUSH_S) не дают ни одного
#: срабатывания, буфер растёт без предела. Через это время документация
#: обещает «не теряет разговор целиком при падении» — обещание не выполнялось
#: бы для этого случая: до первого флаша ничего не лежит на диске.
#:
#: 5 минут — редко задевает обычную речь (пауза длиннее 2с случается почти
#: всегда), но не даёт куску расти неограниченно в патологическом случае.
CHUNK_MAX_WALL_S = 300.0


class TrackRecorder:
    def __init__(self, track: str, chunk_speech_s: float,
                max_wall_s: float = CHUNK_MAX_WALL_S):
        self.track = track
        self.chunk_speech_s = chunk_speech_s
        self.max_wall_s = max_wall_s
        self._frames: list[bytes] = []
        self._offset: float = 0.0
        self._last_at: float = 0.0
        self._speech_s: float = 0.0
        self._silence_s: float = 0.0

    def add(self, pcm: bytes, speech: bool, at: float) -> None:
        if not self._frames:
            if not speech:
                return
            self._offset = at
        self._frames.append(pcm)
        self._last_at = at
        if speech:
            self._speech_s += FRAME_S
            self._silence_s = 0.0
        else:
            self._silence_s += FRAME_S

    @property
    def speech_s(self) -> float:
        return self._speech_s

    @property
    def silence_s(self) -> float:
        return self._silence_s

    def ready(self) -> bool:
        """Кусок готов: набрали норму речи и попали в естественную паузу —
        или накопление тянется дольше предохранителя, паузы не дожидаясь."""
        if not self._frames or self._speech_s <= 0.0:
            return False
        if self._speech_s >= self.chunk_speech_s and self._silence_s >= PAUSE_FLUSH_S:
            return True
        return (self._last_at - self._offset) >= self.max_wall_s

    def take(self) -> tuple[float, bytes] | None:
        """Забрать накопленное. Хвост тишины отбрасываем."""
        if not self._frames or self._speech_s <= 0.0:
            self.reset()
            return None
        keep = len(self._frames) - int(self._silence_s / FRAME_S)
        pcm = b"".join(self._frames[:max(1, keep)])
        offset = self._offset
        self.reset()
        return offset, pcm

    def reset(self) -> None:
        self._frames.clear()
        self._offset = 0.0
        self._speech_s = 0.0
        self._silence_s = 0.0
