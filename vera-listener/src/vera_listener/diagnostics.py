"""Audio pipeline counters that never retain samples or transcripts."""
from __future__ import annotations

from dataclasses import dataclass, field

from vera_listener.gate import MIC, SYSTEM


@dataclass
class TrackActivity:
    frames: int = 0
    vad_frames: int = 0
    asr_attempts: int = 0
    asr_segments: int = 0
    last_frame_at: float | None = None

    def observe(self, at: float, speech: bool) -> None:
        self.frames += 1
        self.vad_frames += int(speech)
        self.last_frame_at = at

    def age(self, now: float) -> float:
        if self.last_frame_at is None:
            return -1.0
        return max(0.0, now - self.last_frame_at)

    def reset_window(self) -> None:
        self.frames = 0
        self.vad_frames = 0


@dataclass
class CaptureActivity:
    tracks: dict[str, TrackActivity] = field(default_factory=lambda: {
        MIC: TrackActivity(), SYSTEM: TrackActivity(),
    })

    def observe(self, track: str, at: float, speech: bool) -> None:
        self.tracks[track].observe(at, speech)

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
