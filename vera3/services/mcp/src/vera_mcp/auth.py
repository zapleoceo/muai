"""Bearer-аутентификация MCP-эндпоинта.

Токен MCP_TOKEN (один) и/или MCP_TOKENS (`claude:xxx,codex:yyy`) — отдельный
секрет, не INTERNAL_SECRET: утечка токена агента не открывает внутренние
эндпоинты сервисов. Имя токена попадает в `mcp_audit.client`.

Fail-closed: ни одного токена в окружении — любой запрос получает 401.
"""
from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Mapping
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

MIN_TOKEN_LEN = 16
DEFAULT_CLIENT = "default"
HEALTH_PATH = "/healthz"
CLIENT_SCOPE_KEY = "mcp_client"


def load_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """{токен: имя клиента}. Слишком короткие токены отбрасываются."""
    env = os.environ if env is None else env
    named: dict[str, str] = {}
    single = (env.get("MCP_TOKEN") or "").strip()
    if single:
        named[single] = DEFAULT_CLIENT
    for i, item in enumerate(filter(None, (p.strip() for p in
                                           (env.get("MCP_TOKENS") or "").split(",")))):
        name, sep, token = item.partition(":")
        if not sep:
            name, token = f"token{i + 1}", item
        named[token.strip()] = name.strip() or f"token{i + 1}"
    good = {t: n for t, n in named.items() if len(t) >= MIN_TOKEN_LEN}
    if len(good) != len(named):
        log.error("MCP: токены короче %d символов отброшены", MIN_TOKEN_LEN)
    return good


def match_token(provided: str | None, tokens: Mapping[str, str]) -> str | None:
    """Имя клиента или None. Сравнивает со ВСЕМИ токенами без раннего выхода."""
    if not provided:
        return None
    found: str | None = None
    for token, name in tokens.items():
        if hmac.compare_digest(provided.encode(), token.encode()):
            found = name
    return found


def bearer_of(headers: list[tuple[bytes, bytes]]) -> str | None:
    for key, value in headers:
        if key.lower() == b"authorization":
            scheme, _, rest = value.decode("latin-1").partition(" ")
            return rest.strip() if scheme.lower() == "bearer" else None
    return None


def client_of(ctx: Any) -> str:
    """Имя клиента, которое middleware положил в scope запроса."""
    return ctx.request_context.request.scope.get(CLIENT_SCOPE_KEY, "unknown")


class BearerAuthMiddleware:
    """Чистый ASGI (не BaseHTTPMiddleware) — не буферизует стримы ответа."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        client = match_token(bearer_of(scope.get("headers", [])), load_tokens())
        if client is None:
            response = JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        scope[CLIENT_SCOPE_KEY] = client
        await self.app(scope, receive, send)
