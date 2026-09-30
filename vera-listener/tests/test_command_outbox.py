"""Очередь поручений: сеть легла — поручение ждёт на диске, а не пропадает."""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from vera_listener.app import Listener
from vera_listener.capture import MIC, SYSTEM
from vera_listener.command_outbox import CommandOutbox
from vera_listener.config import Config, load_config
from vera_listener.transcriber import Segment

CMD = {"command_id": "vc-1", "instruction": "напиши мне", "spoken_at": "x",
       "app": None, "window_title": None}


def _box(tmp_path, results: list[tuple[bool, bool, str]]):
    posted: list[dict] = []

    def post(command):
        posted.append(command)
        return results.pop(0) if results else (True, True, "HTTP 200")

    return CommandOutbox(tmp_path / "queue", post), posted


def test_network_failure_keeps_command_and_retries(tmp_path):
    box, posted = _box(tmp_path, [(False, True, "URLError"), (False, True, "timeout")])
    box.put(CMD)
    assert box.flush() == 1 and box.backoff_s > 0
    assert box.flush() == 1
    assert box.flush() == 0
    assert len(posted) == 3 and box.ready() == []


def test_command_survives_restart(tmp_path):
    box, _ = _box(tmp_path, [(False, True, "URLError")])
    box.put(CMD)
    box.flush()
    again, posted = _box(tmp_path, [])
    assert again.flush() == 0
    assert posted == [CMD]


def test_rejected_command_is_parked_not_retried_forever(tmp_path):
    box, _ = _box(tmp_path, [(False, False, "HTTP 422")])
    box.put(CMD)
    assert box.flush() == 0
    assert list(box.failed_dir.glob("*.json"))


def test_codeword_comes_from_config(tmp_path, monkeypatch):
    monkeypatch.setenv("VERA_CODEWORD", "Вера, запиши")
    monkeypatch.setenv("VERA_LISTENER_ROOT", str(tmp_path))
    assert load_config().codeword == "Вера, запиши"
    assert Config().codeword == "Вера, мне нужна помощь"


class _Transcriber:
    def __init__(self, text: str):
        self.text = text

    def transcribe(self, pcm: bytes, track: str = "") -> list[Segment]:
        return [Segment(at=0.0, end=4.0, text=self.text)]


def _listener(tmp_path, text: str) -> Listener:
    listener = Listener(replace(Config(root=tmp_path, internal_secret="x")))
    listener.transcriber = _Transcriber(text)
    listener.segmenter.feed(0.0, MIC, True, app="zoom.exe", window_title="Созвон")
    listener._ensure_open()
    watch = listener._watches[listener.session]
    for i in range(80):
        watch.hear(MIC, i * 0.5, 0.5, False)
    return listener


def _pcm() -> bytes:
    return np.zeros(16_000 * 4, dtype=np.int16).tobytes()


def test_mic_phrase_reaches_the_command_queue(tmp_path):
    listener = _listener(tmp_path, "Вера, мне нужна помощь, срочно напиши мне")
    listener._transcribe_into(listener.session, MIC, 1.0, _pcm(), None)
    assert len(listener.commands.ready()) == 1


def test_system_phrase_never_reaches_the_command_queue(tmp_path):
    listener = _listener(tmp_path, "Вера, мне нужна помощь, срочно напиши мне")
    listener._transcribe_into(listener.session, SYSTEM, 1.0, _pcm(), None)
    assert listener.commands.ready() == []


def test_switch_off_disables_commands(tmp_path):
    config = replace(Config(root=tmp_path, internal_secret="x"), voice_commands=False)
    listener = Listener(config)
    listener.segmenter.feed(0.0, MIC, True, app="zoom.exe", window_title="Созвон")
    listener._ensure_open()
    assert listener._watches == {}
