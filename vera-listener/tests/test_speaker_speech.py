"""Обрезка тишины перед снятием отпечатка.

Дефект, ради которого это написано, был тихим и дорогим: порог длины мерил
кусок, а не речь в нём. Кусок 3.5с с полутора секундами слов проходил порог,
давал отпечаток с косинусом 0.239 к СВОЕМУ ЖЕ голосу — ниже, чем у чужих
между собой, — и вставал отдельным «собеседником». Вживую 16.09 клиент в
созвоне рассыпался на восемь голосов: сорок реплик в главном кластере и семь
одиночек, все семь — его же короткие фразы.
"""
from __future__ import annotations

import numpy as np

from vera_listener.speakers.speech import keep_speech
from vera_listener.vad import FRAME_SAMPLES, SAMPLE_RATE


def _tone(seconds: float, freq: float = 180.0) -> np.ndarray:
    """Звук, который silero считает речью: голосовой диапазон с гармониками."""
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    wave = sum(np.sin(2 * np.pi * freq * k * t) / k for k in (1, 2, 3, 4))
    return (0.3 * wave).astype(np.float32)


class TestKeepSpeech:
    def test_digital_silence_leaves_nothing(self):
        assert len(keep_speech(np.zeros(SAMPLE_RATE * 2, np.float32))) == 0

    def test_empty_input_does_not_explode(self):
        assert len(keep_speech(np.zeros(0, np.float32))) == 0

    def test_shorter_than_one_frame_leaves_nothing(self):
        assert len(keep_speech(np.zeros(FRAME_SAMPLES - 1, np.float32))) == 0

    def test_trailing_silence_is_dropped(self):
        """Хвост тишины — главный источник разваленных кластеров."""
        piece = np.zeros(int(3.5 * SAMPLE_RATE), np.float32)
        speech = _tone(1.5)
        piece[:len(speech)] = speech

        kept = keep_speech(piece)

        assert len(kept) < len(piece) / 2
        assert len(kept) <= len(speech) + FRAME_SAMPLES

    def test_output_is_a_multiple_of_the_frame(self):
        kept = keep_speech(_tone(2.0))
        assert len(kept) % FRAME_SAMPLES == 0

    def test_detector_state_does_not_leak_between_calls(self):
        """У silero есть история; кусок из середины разговора не должен
        наследовать её от предыдущего — иначе результат зависит от порядка."""
        piece = _tone(1.0)
        assert len(keep_speech(piece)) == len(keep_speech(piece))
