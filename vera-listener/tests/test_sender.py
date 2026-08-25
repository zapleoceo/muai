"""Отправщик: успех чистит очередь, сеть — держит, битое тело — в failed."""
from __future__ import annotations

from vera_listener.config import Config
from vera_listener.outbox import Outbox
from vera_listener.sender import Sender


def _ready(box: Outbox, name: str) -> None:
    path = box.start(name, "2026-08-25T10:00:00", app="zoom.exe",
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "mic", "текст")
    box.finish(path, "2026-08-25T10:01:00")


def _sender(tmp_path, result) -> tuple[Sender, Outbox]:
    box = Outbox(tmp_path / "queue")
    sender = Sender(Config(root=tmp_path, internal_secret="s"), box)
    sender.post = lambda payload: result  # noqa: ARG005
    return sender, box


def test_successful_send_clears_the_queue(tmp_path):
    sender, box = _sender(tmp_path, (True, True, "HTTP 200"))
    _ready(box, "a")
    _ready(box, "b")
    assert sender.flush() == (2, 0)
    assert box.ready() == []


def test_network_failure_keeps_everything_for_later(tmp_path):
    sender, box = _sender(tmp_path, (False, True, "URLError"))
    _ready(box, "a")
    _ready(box, "b")
    sent, left = sender.flush()
    assert (sent, left) == (0, 2)
    assert sender.backoff_s > 0


def test_rejected_body_is_parked_not_retried_forever(tmp_path):
    sender, box = _sender(tmp_path, (False, False, "HTTP 422: bad"))
    _ready(box, "a")
    assert sender.flush() == (0, 0)
    assert (box.failed_dir / "a.jsonl").exists()


def test_backoff_resets_once_the_queue_drains(tmp_path):
    sender, box = _sender(tmp_path, (False, True, "URLError"))
    _ready(box, "a")
    sender.flush()
    assert sender.backoff_s > 0
    sender.post = lambda payload: (True, True, "HTTP 200")  # noqa: ARG005
    sender.flush()
    assert sender.backoff_s == 0.0
