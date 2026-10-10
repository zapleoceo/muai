"""Diagnostics distinguish missing capture, silent VAD, and command outcomes."""
from __future__ import annotations

import logging

from vera_listener.app import Listener
from vera_listener.diagnostics import CaptureActivity, command_stage
from vera_listener.gate import MIC, SYSTEM


def test_command_stage_distinguishes_missing_mic_frames() -> None:
    activity = CaptureActivity()
    activity.observe(SYSTEM, 1.0, True)
    assert command_stage(activity, 0, 0) == "no_mic_frames"


def test_command_stage_distinguishes_mic_frames_without_vad() -> None:
    activity = CaptureActivity()
    activity.observe(MIC, 1.0, False)
    assert command_stage(activity, 0, 0) == "mic_vad_zero"


def test_command_stage_distinguishes_asr_and_codeword_results() -> None:
    activity = CaptureActivity()
    activity.observe(MIC, 1.0, True)
    assert command_stage(activity, 0, 0) == "mic_asr_not_run"
    activity.tracks[MIC].asr_attempts = 1
    assert command_stage(activity, 0, 0) == "mic_asr_no_text"
    activity.tracks[MIC].asr_segments = 1
    assert command_stage(activity, 0, 0) == "no_codeword"
    assert command_stage(activity, 1, 0) == "codeword_not_queued"
    assert command_stage(activity, 1, 1) == "command_queued"


def test_capture_health_reports_stale_track_without_audio(tmp_path, caplog) -> None:
    from vera_listener.config import Config

    listener = Listener(Config(root=tmp_path, internal_secret="test"))
    listener._capture_activity.observe(SYSTEM, 10.0, False)
    listener._capture_report_at = 0.0
    with caplog.at_level(logging.INFO, logger="listener"):
        listener._report_capture(20.0)
    assert "mic_frames=0 mic_vad=0 mic_age_s=-1.0" in caplog.text
    assert "system_frames=1 system_vad=0 system_age_s=10.0" in caplog.text
