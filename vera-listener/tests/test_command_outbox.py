"""Очередь поручений: сеть легла — поручение ждёт на диске, а не пропадает."""
from __future__ import annotations

import os
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from vera_listener.app import Listener
from vera_listener.capture import MIC, SYSTEM
from vera_listener.command_outbox import CommandOutbox
from vera_listener.config import Config, load_config
from vera_listener.transcriber import Segment

CMD = {"command_id": "vc-1", "instruction": "напиши мне", "spoken_at": "x",
       "app": None, "window_title": None}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _box(tmp_path, results, clock=None):
    """results: список ответов или dict command_id → список ответов."""
    posted: list[dict] = []

    def post(command):
        posted.append(command)
        queue = results.get(command["command_id"], []) if isinstance(results, dict) \
            else results
        return queue.pop(0) if queue else (True, True, "HTTP 200")

    return CommandOutbox(tmp_path / "queue", post, clock=clock or _Clock()), posted


def test_network_failure_keeps_command_and_retries(tmp_path):
    clock = _Clock()
    box, posted = _box(tmp_path, [(False, True, "URLError"), (False, True, "timeout")],
                       clock)
    box.put(CMD)
    assert box.flush() == 1 and box.backoff_s > 0
    assert box.flush() == 1 and len(posted) == 1      # пауза ещё не вышла
    clock.now += 100
    assert box.flush() == 1
    clock.now += 100
    assert box.flush() == 0
    assert len(posted) == 3 and box.ready() == []


@pytest.mark.parametrize("code", [401, 403, 404, 500, 503])
def test_fixable_server_answers_are_retried_not_parked(tmp_path, code):
    """404 — шлюз ещё без новой ручки, 401/403 — ротация секрета."""
    box, _ = _box(tmp_path, [(False, False, f"HTTP {code}: x")])
    box.put(CMD)
    assert box.flush() == 1
    assert list(box.failed_dir.glob("*.json")) == []


@pytest.mark.parametrize("code", [400, 413, 422])
def test_poison_answers_are_parked(tmp_path, code):
    box, _ = _box(tmp_path, [(False, False, f"HTTP {code}: x")])
    box.put(CMD)
    assert box.flush() == 0
    assert list(box.failed_dir.glob("*.json"))


def test_one_stuck_command_does_not_block_the_rest(tmp_path):
    stuck = [(False, True, "HTTP 503")] * 10
    box, posted = _box(tmp_path, {"vc-1": stuck})
    box.put(CMD)
    box.put({**CMD, "command_id": "vc-2"})
    assert box.flush() == 1
    assert [c["command_id"] for c in posted] == ["vc-1", "vc-2"]
    assert [p.stem for p in box.ready()] == ["vc-1"]


def test_old_parked_commands_are_pruned(tmp_path):
    box, _ = _box(tmp_path, [])
    old = box.failed_dir / "vc-old.json"
    fresh = box.failed_dir / "vc-new.json"
    old.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    week_ago = time.time() - 8 * 24 * 3600
    os.utime(old, (week_ago, week_ago))
    assert box.prune_failed() == 1
    assert not old.exists() and fresh.exists()


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
        watch.hear(SYSTEM, i * 0.5, 0.5, False)
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


def test_server_body_is_not_logged_at_warning(tmp_path, caplog):
    box, _ = _box(tmp_path, {"vc-1": [(False, False, "HTTP 422: {'input': 'напиши мне'}")],
                             "vc-2": [(False, True, "HTTP 503: напиши мне")]})
    box.put(CMD)
    box.put({**CMD, "command_id": "vc-2"})
    with caplog.at_level("WARNING", logger="listener.command_outbox"):
        box.flush()
        box.flush()
    warnings = " ".join(r.getMessage() for r in caplog.records)
    assert "HTTP 422" in warnings and "HTTP 503" in warnings
    assert "напиши" not in warnings


def test_watch_is_dropped_even_if_closing_the_session_fails(tmp_path, monkeypatch):
    listener = _listener(tmp_path, "просто разговор")
    path = listener.session

    def boom(*_a, **_k):
        raise RuntimeError("judge упал")

    monkeypatch.setattr("vera_listener.app.judge", boom)
    closed = SimpleNamespace(session=SimpleNamespace(app="zoom.exe"))
    with pytest.raises(RuntimeError):
        listener._run_job(("close", path, closed, {}, None, [], 0.0, None))
    assert path not in listener._watches
