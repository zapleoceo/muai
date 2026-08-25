"""Накопитель аудио одной дорожки между отправками в распознавание.

Тишина до первой речи не копится, хвост тишины обрезается: держать в памяти
паузы незачем, а распознавание на них только тратит процессор.
"""
from __future__ import annotations

from vera_listener.vad import FRAME_S

PAUSE_FLUSH_S = 2.0


class TrackRecorder:
    def __init__(self, track: str, chunk_speech_s: float):
        self.track = track
        self.chunk_speech_s = chunk_speech_s
        self._frames: list[bytes] = []
        self._offset: float = 0.0
        self._speech_s: float = 0.0
        self._silence_s: float = 0.0

    def add(self, pcm: bytes, speech: bool, at: float) -> None:
        if not self._frames:
            if not speech:
                return
            self._offset = at
        self._frames.append(pcm)
        if speech:
            self._speech_s += FRAME_S
            self._silence_s = 0.0
        else:
            self._silence_s += FRAME_S

    @property
    def speech_s(self) -> float:
        return self._speech_s

    def ready(self) -> bool:
        """Кусок готов: набрали норму речи и попали в естественную паузу."""
        if not self._frames or self._speech_s <= 0.0:
            return False
        return (self._speech_s >= self.chunk_speech_s
                and self._silence_s >= PAUSE_FLUSH_S)

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
