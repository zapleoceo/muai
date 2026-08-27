"""Локальное распознавание (faster-whisper, int8 на CPU).

Модель грузится лениво и живёт до конца процесса: в тишине она не нужна,
а держать её в памяти круглые сутки незачем на 8-ядерном ноутбуке.
Распознаём кусками по ходу разговора, а не одним пакетом в конце — иначе
часовой созвон целиком висел бы в оперативке и терялся при падении.
"""
from __future__ import annotations

import logging

import numpy as np

from vera_listener.config import Config

log = logging.getLogger("listener.stt")

#: Короче этого распознавать нечего: на обрывке в 64 мс whisper выдаёт либо
#: пустоту, либо выдумку («Спасибо за просмотр»). В логе 2026-08-27 таких
#: запусков было шесть за два часа — все на закрытии пустых сессий.
MIN_AUDIO_S = 0.6


class Transcriber:
    def __init__(self, config: Config):
        self.config = config
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            log.info("гружу модель %s (%s, %d потока)", self.config.model_size,
                     self.config.compute_type, self.config.cpu_threads)
            self._model = WhisperModel(
                self.config.model_size, device="cpu",
                compute_type=self.config.compute_type,
                cpu_threads=self.config.cpu_threads,
            )
        return self._model

    def transcribe(self, pcm: bytes) -> list[tuple[float, str]]:
        """PCM16 16 кГц → [(смещение от начала куска, текст)]."""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if len(audio) < MIN_AUDIO_S * 16_000:
            return []
        segments, _info = self._load().transcribe(
            audio,
            language=self.config.language,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        out: list[tuple[float, str]] = []
        for segment in segments:
            text = segment.text.strip()
            if text:
                out.append((float(segment.start), text))
        return out
