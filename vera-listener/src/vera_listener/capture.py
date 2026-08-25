"""Захват двух дорожек: микрофон и системный вывод (WASAPI loopback).

Дорожки принципиально раздельные до самого конца: по ним же различаются
«я» и «собеседник» на сервере, поэтому смешивать их нельзя.

Устройство может исчезнуть — воткнули наушники, ушли в другую сессию
Windows, уснул ноутбук. Это не ошибка: поток просто переоткрывается, а
слушатель продолжает жить.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np
import soundcard as sc

from vera_listener.vad import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger("listener.capture")

MIC = "mic"
SYSTEM = "system"
REOPEN_PAUSE_S = 5.0


@dataclass(frozen=True)
class Frame:
    track: str
    at: float
    pcm: bytes


def to_pcm16(block: np.ndarray) -> bytes:
    """float32 [-1, 1] любой канальности → моно int16."""
    mono = block.mean(axis=1) if block.ndim > 1 else block
    clipped = np.clip(mono, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


class Capture:
    """Два потока-читателя, общая очередь кадров по 32 мс."""

    def __init__(self, frames: queue.Queue[Frame]):
        self.frames = frames
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.device_hint: str | None = None

    def start(self) -> None:
        for track in (MIC, SYSTEM):
            thread = threading.Thread(target=self._run, args=(track,),
                                      name=f"capture-{track}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)

    def _open(self, track: str):
        if track == MIC:
            return sc.default_microphone()
        speaker = sc.default_speaker()
        self.device_hint = str(speaker.name)
        return sc.get_microphone(id=str(speaker.name), include_loopback=True)

    def _run(self, track: str) -> None:
        while not self._stop.is_set():
            try:
                device = self._open(track)
                with device.recorder(samplerate=SAMPLE_RATE, channels=1,
                                     blocksize=FRAME_SAMPLES) as recorder:
                    log.info("дорожка %s: %s", track, device.name)
                    while not self._stop.is_set():
                        block = recorder.record(numframes=FRAME_SAMPLES)
                        self.frames.put(Frame(track, time.monotonic(), to_pcm16(block)))
            except Exception as e:
                # Устройство пропало или занято чужой сессией Windows —
                # ждём и пробуем снова, вместо того чтобы уронить слушателя.
                if not self._stop.is_set():
                    log.warning("дорожка %s недоступна (%s) — переоткрою через %.0fс",
                                track, e, REOPEN_PAUSE_S)
                    time.sleep(REOPEN_PAUSE_S)
