"""Режим poll: исходящий длинный запрос room_inbox_wait, без туннеля и вебхука.

Цикл держит запросы по `WAIT_S` до общего срока и выходит, как только сервер отдал
непрочитанное. Обрыв связи, 5xx и 429 — повтор с растущей паузой; 401/403 — сразу
выход: повтор с тем же токеном не поможет. Дублей нет: точка отсчёта — либо курсор
агента на сервере (двигает room_ack), либо явный `after`, и сервер отдаёт только id > неё.
"""
from __future__ import annotations

import http.client
import socket
import sys
import time
import urllib.error
from collections.abc import Callable
from typing import Any

from room_mcp_client import ToolError, call_tool

WAIT_S = 80
BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
EXIT_OK, EXIT_AUTH, EXIT_TIMEOUT = 0, 2, 3
_FATAL_HTTP = (401, 403)

Call = Callable[[str, str, str, dict[str, Any], float], dict[str, Any]]


def _arguments(after: int | None, wait_s: int, to: str | None) -> dict[str, Any]:
    args: dict[str, Any] = {"timeout_s": wait_s, "exclude_system": True}
    if after is not None:
        args["after_id"] = after
    if to is not None:
        args["to"] = to
    return args


def poll(url: str, token: str, after: int | None, timeout: float, to: str | None = None,
         call: Call = call_tool, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> tuple[int, dict[str, Any] | None]:
    deadline = clock() + timeout
    failures = 0
    while (remaining := deadline - clock()) > 0:
        wait_s = max(1, min(WAIT_S, int(remaining)))
        try:
            result = call(url, token, "room_inbox_wait", _arguments(after, wait_s, to),
                          wait_s + 20.0)
        except urllib.error.HTTPError as e:
            if e.code in _FATAL_HTTP:
                print(f"poll: HTTP {e.code}, check ROOM_MCP_TOKEN", file=sys.stderr)
                return EXIT_AUTH, None
            failures += 1
            print(f"poll: HTTP {e.code}, retry", file=sys.stderr)
        except (urllib.error.URLError, http.client.HTTPException, socket.timeout,
                ConnectionError, TimeoutError, ToolError, ValueError) as e:
            failures += 1
            print(f"poll: {type(e).__name__}, retry", file=sys.stderr)
        else:
            failures = 0
            if result.get("messages"):
                return EXIT_OK, result
            continue
        sleep(min(BACKOFF_S[min(failures, len(BACKOFF_S)) - 1], max(0.0, deadline - clock())))
    return EXIT_TIMEOUT, None
