from __future__ import annotations

import base64
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from standardwebhooks import Webhook

SECRET = "whsec_" + base64.b64encode(bytes(range(32))).decode()
BODY = b'{"type":"room.message","room":"main"}'


def _listener() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "room_push_listener.py"
    spec = importlib.util.spec_from_file_location("room_push_listener", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signed(now: datetime, body: bytes = BODY) -> dict[str, str]:
    msg_id = "room-msg-9"
    sig = Webhook(SECRET).sign(msg_id, now, body.decode())
    return {"webhook-id": msg_id, "webhook-timestamp": str(int(now.timestamp())),
            "webhook-signature": sig}


def test_verify_accepts_standard_webhooks_signature() -> None:
    mod = _listener()
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    assert mod.verify(SECRET, _signed(now), BODY, now.timestamp())


def test_verify_rejects_tampered_body_and_wrong_secret() -> None:
    mod = _listener()
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    headers = _signed(now)
    assert not mod.verify(SECRET, headers, BODY + b" ", now.timestamp())
    other = "whsec_" + base64.b64encode(bytes(range(1, 33))).decode()
    assert not mod.verify(other, headers, BODY, now.timestamp())


def test_verify_rejects_stale_timestamp_and_missing_headers() -> None:
    mod = _listener()
    now = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    headers = _signed(now)
    assert not mod.verify(SECRET, headers, BODY, now.timestamp() + 3600)
    assert not mod.verify(SECRET, {}, BODY, now.timestamp())
