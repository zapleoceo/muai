"""Короткая просьба уходит через секунды после паузы, а не на закрытии сессии.

Обычный кусок микрофона ждёт минуту речи, а сессия закрывается после 60 с
тишины. Без раннего флаша по паузе «Вера, мне нужна помощь, …» распознавалась
бы только на закрытии сессии.
"""
from __future__ import annotations

from dataclasses import replace

from vera_listener.app import Listener
from vera_listener.capture import MIC, SYSTEM, Frame
from vera_listener.config import Config
from vera_listener.recorder import PAUSE_FLUSH_S
from vera_listener.stitch import Segment
from vera_listener.vad import FRAME_S

PHRASE = "Вера, мне нужна помощь, срочно напиши мне что-то в телеграм"
PCM = b"\x00\x00" * 512


class _Stt:
    def transcribe(self, pcm: bytes, track: str = "") -> list[Segment]:
        if track != MIC:
            return []
        return [Segment(at=0.0, end=3.0, text=PHRASE)]


def _listener(tmp_path, *, commands: bool = True) -> Listener:
    config = replace(Config(root=tmp_path, internal_secret="x"),
                     voice_commands=commands)
    listener = Listener(config)
    listener.transcriber = _Stt()
    listener.segmenter.feed(0.0, MIC, True, app="zoom.exe", window_title="окно")
    listener._ensure_open()
    return listener


def _play(listener: Listener, start: float, until: float, *, mic_speech: bool) -> float:
    at = start
    while at < until:
        listener._record(Frame(MIC, at, PCM), mic_speech)
        listener._record(Frame(SYSTEM, at, PCM), False)
        at += FRAME_S
    return at


def _mic_chunks(listener: Listener) -> list[tuple]:
    jobs = []
    while not listener.jobs.empty():
        job = listener.jobs.get()
        if job[0] == "chunk" and job[2] == MIC:
            jobs.append(job)
    return jobs


def test_mic_chunk_flushes_on_pause_while_commands_are_on(tmp_path):
    listener = _listener(tmp_path)
    at = _play(listener, 0.0, 3.0, mic_speech=True)
    at = _play(listener, at, at + PAUSE_FLUSH_S - 0.2, mic_speech=False)
    assert _mic_chunks(listener) == []
    _play(listener, at, at + 0.3, mic_speech=False)
    assert len(_mic_chunks(listener)) == 1
    assert listener.session is not None


def test_command_is_queued_seconds_after_the_pause(tmp_path):
    listener = _listener(tmp_path)
    at = _play(listener, 0.0, 3.0, mic_speech=True)
    at = _play(listener, at, at + PAUSE_FLUSH_S + 0.1, mic_speech=False)
    for job in _mic_chunks(listener):
        listener._run_job(job)
    assert listener.commands.ready() == []
    # Ступень VAD ждёт окна ±6 с вокруг фразы и секунду запаса на очерёдность.
    _play(listener, at, 11.0, mic_speech=False)
    listener._tick_watches()
    assert listener.session is not None
    assert len(listener.commands.ready()) == 1


def test_no_early_mic_flush_when_commands_are_off(tmp_path):
    listener = _listener(tmp_path, commands=False)
    at = _play(listener, 0.0, 3.0, mic_speech=True)
    _play(listener, at, at + PAUSE_FLUSH_S + 0.5, mic_speech=False)
    assert _mic_chunks(listener) == []
