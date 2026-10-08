"""Bearer-аутентификация MCP-эндпоинта.

Токен MCP_TOKEN (один) и/или MCP_TOKENS (`claude:xxx,codex:yyy`) — отдельный
секрет, не INTERNAL_SECRET: утечка токена агента не открывает внутренние
эндпоинты сервисов. Имя токена попадает в `mcp_audit.client`.

Fail-closed: ни одного токена в окружении — любой запрос получает 401.

ROOM_TOKENS (тот же формат) — токены комнаты агентов: запрос с таким токеном
уходит на отдельный MCP-сервер, где есть только room_*-инструменты, без личной
памяти. Один и тот же токен в обоих наборах — ошибка старта (неясно, куда вести).
"""
from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Mapping
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

log = logging.getLogger(__name__)

MIN_TOKEN_LEN = 32
DEFAULT_CLIENT = "default"
HEALTH_PATH = "/healthz"
CLIENT_SCOPE_KEY = "mcp_client"
REALM_SCOPE_KEY = "mcp_realm"
VERA_REALM = "vera"
ROOM_REALM = "room"


class WeakTokenError(ValueError):
    """Токен короче MIN_TOKEN_LEN: сервис не должен стартовать с таким."""


def _pairs(raw: str | None) -> list[tuple[str, str]]:
    pairs = []
    for i, item in enumerate(filter(None, (p.strip() for p in (raw or "").split(",")))):
        name, sep, token = item.partition(":")
        if not sep:
            name, token = f"token{i + 1}", item
        pairs.append((name.strip() or f"token{i + 1}", token.strip()))
    return pairs


def _parse_list(raw: str | None, named: dict[str, str]) -> dict[str, str]:
    for name, token in _pairs(raw):
        named[token] = name
    return named


def _parse(env: Mapping[str, str]) -> dict[str, str]:
    named: dict[str, str] = {}
    single = (env.get("MCP_TOKEN") or "").strip()
    if single:
        named[single] = DEFAULT_CLIENT
    return _parse_list(env.get("MCP_TOKENS"), named)


def _parse_room(env: Mapping[str, str]) -> dict[str, str]:
    return _parse_list(env.get("ROOM_TOKENS"), {})


def load_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """{токен: имя клиента}. Короткие токены отбрасываются (при старте их
    ловит `validate_tokens`, здесь — защита на случай смены env на лету)."""
    named = _parse(os.environ if env is None else env)
    return {t: n for t, n in named.items() if len(t) >= MIN_TOKEN_LEN}


def validate_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Как load_tokens, но короткий токен — исключение с именем клиента."""
    named = _parse(os.environ if env is None else env)
    weak = sorted(n for t, n in named.items() if len(t) < MIN_TOKEN_LEN)
    if weak:
        raise WeakTokenError(
            f"MCP tokens for {', '.join(weak)} are shorter than {MIN_TOKEN_LEN} "
            "characters; generate with `openssl rand -hex 32`")
    return named


def load_room_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    named = _parse_room(os.environ if env is None else env)
    return {t: n for t, n in named.items() if len(t) >= MIN_TOKEN_LEN}


def validate_room_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Короткий токен или токен, совпавший с MCP_TOKEN(S), — исключение."""
    source = os.environ if env is None else env
    named = _parse_room(source)
    tokens = [t for _, t in _pairs(source.get("ROOM_TOKENS"))]
    if len(tokens) != len(set(tokens)):
        raise ValueError("ROOM_TOKENS lists the same token twice: the author name would be "
                         "ambiguous")
    weak = sorted(n for t, n in named.items() if len(t) < MIN_TOKEN_LEN)
    if weak:
        raise WeakTokenError(
            f"ROOM tokens for {', '.join(weak)} are shorter than {MIN_TOKEN_LEN} "
            "characters; generate with `openssl rand -hex 32`")
    shared = sorted(named[t] for t in set(named) & set(_parse(source)))
    if shared:
        raise ValueError(f"ROOM_TOKENS reuse an MCP token ({', '.join(shared)}): "
                         "a room token must not open the owner's memory")
    return named


def allowed_rooms(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Комнаты, открытые токенам комнаты (ROOM_NAMES, по умолчанию только main).
    Пока доступ общий для всех ROOM_TOKENS: привязки токена к комнате нет."""
    raw = (os.environ if env is None else env).get("ROOM_NAMES") or "main"
    return frozenset(filter(None, (r.strip() for r in raw.split(","))))


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

    def __init__(self, app: ASGIApp, room_oauth: Any = None) -> None:
        self.app = app
        self.room_oauth = room_oauth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or (
                scope["type"] == "http" and scope.get("path") == HEALTH_PATH):
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # websocket и любой будущий тип сюда не ходят: закрываем, не пропуская
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        path = scope.get("path", "")
        if self.room_oauth is not None and (path in {
                "/.well-known/oauth-authorization-server",
                "/.well-known/oauth-protected-resource/mcp",
                "/authorize", "/token", "/register", "/revoke",
                "/oauth/internal/consent"}):
            scope[REALM_SCOPE_KEY] = ROOM_REALM
            await self.app(scope, receive, send)
            return
        provided = bearer_of(scope.get("headers", []))
        client = match_token(provided, load_tokens())
        room_client = match_token(provided, load_room_tokens())
        if room_client is None and self.room_oauth is not None and provided:
            grant = await self.room_oauth.load_access_token(provided)
            if grant is not None:
                room_client = grant.subject
        if client is None and room_client is None:
            challenge = 'Bearer'
            if self.room_oauth is not None:
                challenge = ('Bearer resource_metadata="https://dima.veranda.my/'
                             '.well-known/oauth-protected-resource/mcp"')
            response = JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": challenge})
            await response(scope, receive, send)
            return
        # Совпадение с обоими наборами отсекает validate_room_tokens при старте;
        # если env сменили на лету — побеждает узкий мир комнаты, не личная память.
        if room_client is not None:
            scope[CLIENT_SCOPE_KEY], scope[REALM_SCOPE_KEY] = room_client, ROOM_REALM
        else:
            scope[CLIENT_SCOPE_KEY], scope[REALM_SCOPE_KEY] = client, VERA_REALM
        await self.app(scope, receive, send)
