"""Audio pipeline counters that never retain samples or transcripts."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from vera_listener.gate import MIC, SYSTEM


def to_dbfs(level: float) -> float:
    """Уровень int16 → dBFS; полная тишина — -120, чтобы в логе было число."""
    return 20 * math.log10(level / 32767) if level > 0 else -120.0


@dataclass
class TrackActivity:
    frames: int = 0
    vad_frames: int = 0
    asr_attempts: int = 0
    asr_segments: int = 0
    last_frame_at: float | None = None
    peak: int = 0
    sum_sq: float = 0.0
    samples: int = 0

    def observe(self, at: float, speech: bool, pcm: bytes = b"") -> None:
        self.frames += 1
        self.vad_frames += int(speech)
        self.last_frame_at = at
        if pcm:
            level = np.frombuffer(pcm, dtype=np.int16).astype(np.int32)
            self.peak = max(self.peak, int(np.abs(level).max()))
            self.sum_sq += float(np.square(level, dtype=np.int64).sum())
            self.samples += level.size

    def peak_dbfs(self) -> float:
        return to_dbfs(self.peak)

    def rms_dbfs(self) -> float:
        return to_dbfs(math.sqrt(self.sum_sq / self.samples)) if self.samples else to_dbfs(0)

    def age(self, now: float) -> float:
        if self.last_frame_at is None:
            return -1.0
        return max(0.0, now - self.last_frame_at)

    def reset_window(self) -> None:
        self.frames = 0
        self.vad_frames = 0
        self.peak = 0
        self.sum_sq = 0.0
        self.samples = 0


@dataclass
class CaptureActivity:
    tracks: dict[str, TrackActivity] = field(default_factory=lambda: {
        MIC: TrackActivity(), SYSTEM: TrackActivity(),
    })

    def observe(self, track: str, at: float, speech: bool, pcm: bytes = b"") -> None:
        self.tracks[track].observe(at, speech, pcm)

    def reset_window(self) -> None:
        for track in self.tracks.values():
            track.reset_window()


def command_stage(activity: CaptureActivity, hits: int, queued: int) -> str:
    mic = activity.tracks[MIC]
    if not mic.frames:
        return "no_mic_frames"
    if not mic.vad_frames:
        return "mic_vad_zero"
    if not mic.asr_attempts:
        return "mic_asr_not_run"
    if not mic.asr_segments:
        return "mic_asr_no_text"
    if not hits:
        return "no_codeword"
    if not queued:
        return "codeword_not_queued"
    return "command_queued"
