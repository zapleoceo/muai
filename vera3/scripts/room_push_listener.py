"""Локальный слушатель push-сообщений комнаты: принимает подписанный вебхук и отвечает через room_post.

Только стандартная библиотека. Сервер шлёт сообщение на публичный HTTPS-адрес, поэтому
слушателю нужен туннель к 127.0.0.1 (см. docs/agent-room.md). Секреты берутся из
окружения и не печатаются.

  ROOM_PUSH_SECRET  whsec_… из ROOM_PUSH_TARGETS для этого агента
  ROOM_MCP_TOKEN    токен агента в ROOM_TOKENS (автор ответов)
  VERA_MCP_URL      по умолчанию https://dima.veranda.my/mcp
  ROOM_PUSH_INBOX   файл JSONL для принятых сообщений

  python room_push_listener.py serve [--port 8765]
  python room_push_listener.py reply --to dot "текст ответа"
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
import urllib.request
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

TOLERANCE_S = 300
MAX_BODY = 262_144
DEFAULT_INBOX = Path.home() / "vera-room-inbox.jsonl"
DEFAULT_MCP_URL = "https://dima.veranda.my/mcp"
PROTOCOL_VERSION = "2025-06-18"


def verify(secret: str, headers: Mapping[str, str], body: bytes, now: float) -> bool:
    """Standard Webhooks v1: HMAC-SHA256 по `id.timestamp.body`, ключ — base64 после whsec_."""
    msg_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    signature = headers.get("webhook-signature")
    if not (msg_id and timestamp and signature):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(now - ts) > TOLERANCE_S:
        return False
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return any(hmac.compare_digest(expected, part[3:]) for part in signature.split()
               if part.startswith("v1,"))


def _seen_ids(inbox: Path) -> set[str]:
    if not inbox.exists():
        return set()
    ids: set[str] = set()
    for line in inbox.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ids.add(json.loads(line)["event_id"])
    return ids


def _make_handler(secret: str, inbox: Path, seen: set[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            if not verify(secret, self.headers, body, time.time()):
                self.send_error(401)
                return
            event_id = self.headers["webhook-id"]
            if event_id not in seen:
                record = {"event_id": event_id, "received_at": time.time(),
                          "payload": json.loads(body)}
                with inbox.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                seen.add(event_id)
            self.send_response(204)
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"listener {self.address_string()} {fmt % args}\n")

    return Handler


def serve(port: int, inbox: Path) -> None:
    secret = os.environ["ROOM_PUSH_SECRET"]
    handler = _make_handler(secret, inbox, _seen_ids(inbox))
    with HTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"room push listener on 127.0.0.1:{port}, inbox {inbox}", flush=True)
        httpd.serve_forever()


def _rpc(url: str, token: str, payload: dict[str, Any], session: str | None
         ) -> tuple[str | None, bytes]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.headers.get("Mcp-Session-Id"), resp.read()


def room_post(url: str, token: str, body: str, to: str, room: str) -> dict[str, Any]:
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "room-push-listener", "version": "1"}}}
    session, _ = _rpc(url, token, init, None)
    _rpc(url, token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "room_post", "arguments": {"body": body, "to": to, "room": room,
                                           "message_id": f"listener-{uuid.uuid4().hex[:16]}"}}}
    _, raw = _rpc(url, token, call, session)
    return json.loads(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    srv = sub.add_parser("serve")
    srv.add_argument("--port", type=int, default=8765)
    rep = sub.add_parser("reply")
    rep.add_argument("text")
    rep.add_argument("--to", required=True)
    rep.add_argument("--room", default="main")
    args = parser.parse_args()
    inbox = Path(os.environ.get("ROOM_PUSH_INBOX", DEFAULT_INBOX))
    if args.cmd == "serve":
        serve(args.port, inbox)
        return
    url = os.environ.get("VERA_MCP_URL", DEFAULT_MCP_URL)
    result = room_post(url, os.environ["ROOM_MCP_TOKEN"], args.text, args.to, args.room)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
