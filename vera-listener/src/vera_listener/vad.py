"""Гейт речи на silero-vad (ONNX). Кадр 32 мс, 16 кГц, моно.

Пока в кадре нет речи — распознавание не запускается вообще. Именно этот
гейт делает слушателя дешёвым: в тишине работает только он.
"""
from __future__ import annotations

from pysilero_vad import SileroVoiceActivityDetector

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_S = FRAME_SAMPLES / SAMPLE_RATE


class SpeechDetector:
    """Одна независимая копия VAD на дорожку — у них своя история."""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._vad = SileroVoiceActivityDetector()

    def is_speech(self, frame: bytes) -> bool:
        if len(frame) != FRAME_BYTES:
            return False
        return float(self._vad(frame)) >= self.threshold

    def reset(self) -> None:
        self._vad.reset()
