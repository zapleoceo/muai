"""Локальный слушатель сообщений комнаты: длинный опрос (poll) или подписанный вебхук (serve).

Только стандартная библиотека. Основной режим — `poll`: слушатель сам держит исходящий
запрос room_inbox_wait к Вере, туннель и whsec не нужны. Режим `serve` принимает вебхук
ROOM_PUSH_TARGETS и требует туннель к 127.0.0.1 (см. docs/agent-room.md). Секреты
берутся из окружения и не печатаются.

  ROOM_MCP_TOKEN    токен агента в ROOM_TOKENS (poll и автор ответов)
  VERA_MCP_URL      по умолчанию https://dima.veranda.my/mcp
  ROOM_PUSH_SECRET  whsec_… из ROOM_PUSH_TARGETS (только serve/wait)
  ROOM_PUSH_INBOX   файл JSONL для принятых вебхуком сообщений
  ROOM_ENV_FILE     локальный файл с этими ключами, по умолчанию ~/.vera/room.env

  python room_push_listener.py poll [--after N] [--timeout 3600] [--to claude]
  python room_push_listener.py serve [--port 8765]
  python room_push_listener.py wait [--after N] [--timeout 3600]
  python room_push_listener.py reply --to dot "текст ответа"

`poll` завершается с непрочитанными сообщениями (код 0), по таймауту (3) или при
отказе токена (2); после обработки — room_ack, иначе следующий poll вернёт их снова.

`wait` блокируется до прихода нового сообщения и завершается с ним (код 0) или по
таймауту без сообщений (код 3). Запущенный фоном из сессии Claude Code, он будит её
ровно в момент прихода: опроса и трат токенов на ожидание нет.
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
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from room_env import load_env, require
from room_mcp_client import call_tool
from room_poll import poll
from room_push_inbox import Inbox

TOLERANCE_S = 300
MAX_BODY = 262_144
DEFAULT_INBOX = Path.home() / "vera-room-inbox.jsonl"
DEFAULT_MCP_URL = "https://dima.veranda.my/mcp"
MAX_WAIT_S = 3600.0
WAIT_SECRET_HEADER = "X-Room-Push-Secret"
EXIT_TIMEOUT = 3


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


def make_handler(secret: str, inbox: Inbox) -> type[BaseHTTPRequestHandler]:
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
            inbox.add(self.headers["webhook-id"], json.loads(body))
            self.send_response(204)
            self.end_headers()

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            # The tunnel exposes this port publicly, so reading the inbox needs the secret.
            given = self.headers.get(WAIT_SECRET_HEADER, "")
            if url.path != "/wait" or not hmac.compare_digest(given, secret):
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(url.query)
            after = int(query["after"][0]) if "after" in query else inbox.latest
            timeout = min(float(query.get("timeout", [MAX_WAIT_S])[0]), MAX_WAIT_S)
            records = inbox.wait_after(after, timeout)
            body = json.dumps({"latest": inbox.latest, "messages": records},
                              ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"listener {self.address_string()} {fmt % args}\n")

    return Handler


def serve(port: int, inbox_path: Path) -> None:
    secret = require("ROOM_PUSH_SECRET")
    handler = make_handler(secret, Inbox(inbox_path))
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"room push listener on 127.0.0.1:{port}, inbox {inbox_path}", flush=True)
        httpd.serve_forever()


def format_message(record: dict[str, Any]) -> str:
    m = record["payload"]["message"]
    task = f" task={m['task_id']}" if m.get("task_id") else ""
    return f"#{m['id']} {m['from']} -> {m['to']} [{m['status']}]{task}: {m['body']}"


def wait(port: int, after: int | None, timeout: float) -> int:
    query = {"timeout": str(timeout)} | ({"after": str(after)} if after is not None else {})
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/wait?{urllib.parse.urlencode(query)}",
        headers={WAIT_SECRET_HEADER: require("ROOM_PUSH_SECRET")})
    with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
        result = json.loads(resp.read())
    for record in result["messages"]:
        print(format_message(record))
    print(f"latest={result['latest']}")
    return 0 if result["messages"] else EXIT_TIMEOUT


def room_post(url: str, token: str, body: str, to: str, room: str) -> dict[str, Any]:
    return call_tool(url, token, "room_post", {
        "body": body, "to": to, "room": room,
        "message_id": f"listener-{uuid.uuid4().hex[:16]}"})


def run_poll(url: str, token: str, after: int | None, timeout: float, to: str | None) -> int:
    code, result = poll(url, token, after, timeout, to)
    if result is not None:
        for message in result["messages"]:
            print(format_message({"payload": {"message": message}}))
        print(f"latest={result['messages'][-1]['id']}")
    return code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    srv = sub.add_parser("serve")
    srv.add_argument("--port", type=int, default=8765)
    wai = sub.add_parser("wait")
    wai.add_argument("--port", type=int, default=8765)
    wai.add_argument("--after", type=int, default=None)
    wai.add_argument("--timeout", type=float, default=MAX_WAIT_S)
    pol = sub.add_parser("poll")
    pol.add_argument("--after", type=int, default=None)
    pol.add_argument("--timeout", type=float, default=MAX_WAIT_S)
    pol.add_argument("--to", default=None)
    rep = sub.add_parser("reply")
    rep.add_argument("text")
    rep.add_argument("--to", required=True)
    rep.add_argument("--room", default="main")
    args = parser.parse_args()
    load_env()
    inbox = Path(os.environ.get("ROOM_PUSH_INBOX", DEFAULT_INBOX))
    if args.cmd == "serve":
        serve(args.port, inbox)
        return
    if args.cmd == "wait":
        sys.exit(wait(args.port, args.after, min(args.timeout, MAX_WAIT_S)))
    url = os.environ.get("VERA_MCP_URL", DEFAULT_MCP_URL)
    if args.cmd == "poll":
        sys.exit(run_poll(url, require("ROOM_MCP_TOKEN"), args.after, args.timeout, args.to))
    result = room_post(url, require("ROOM_MCP_TOKEN"), args.text, args.to, args.room)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
