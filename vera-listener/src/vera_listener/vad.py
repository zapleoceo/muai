"""Гейт речи на silero-vad (ggml). Кадр 32 мс, 16 кГц, моно.

Пока в кадре нет речи — распознавание не запускается вообще. Именно этот
гейт делает слушателя дешёвым: в тишине работает только он.
"""
from __future__ import annotations

import numpy as np
from pysilero_vad import SileroVoiceActivityDetector

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_S = FRAME_SAMPLES / SAMPLE_RATE

#: Ниже этого уровня нейросеть не зовём: она и так ответит «не речь», а стоит
#: 16% расхода слушателя (профиль живого процесса). Порог нарочно у самого нуля,
#: ≈-60 dBFS: он должен ловить ЦИФРОВУЮ тишину, а не тихую комнату. Выигрыш
#: почти весь на системной дорожке — когда ничего не играет, там ровные нули,
#: то есть половина всех кадров.
SILENCE_LEVEL = 32


class SpeechDetector:
    """Одна независимая копия VAD на дорожку — у них своя история."""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._vad = SileroVoiceActivityDetector()

    def is_speech(self, frame: bytes) -> bool:
        if len(frame) != FRAME_BYTES:
            return False
        samples = np.frombuffer(frame, dtype=np.int16)
        if int(np.abs(samples).max()) < SILENCE_LEVEL:
            return False
        return float(self._vad(frame)) >= self.threshold

    def reset(self) -> None:
        self._vad.reset()
