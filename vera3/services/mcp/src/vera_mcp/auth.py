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
from vera_shared.office.acl import (
    OfficePrincipal,
    check_disjoint,
    parse_registry,
    resolve,
)
from vera_shared.token_lists import (
    MIN_TOKEN_LEN,
    is_strong_token,
    repeated_tokens,
    split_token_entries,
)

log = logging.getLogger(__name__)

DEFAULT_CLIENT = "default"
HEALTH_PATH = "/healthz"
CLIENT_SCOPE_KEY = "mcp_client"
REALM_SCOPE_KEY = "mcp_realm"
VERA_REALM = "vera"
ROOM_REALM = "room"
OFFICE_REALM = "office"
OFFICE_PREFIX = "/office/v1/"
OFFICE_PRINCIPAL_KEY = "office_principal"


class WeakTokenError(ValueError):
    """Токен короче MIN_TOKEN_LEN: сервис не должен стартовать с таким."""


def _pairs(raw: str | None) -> list[tuple[str, str]]:
    pairs = []
    for i, parts in enumerate(split_token_entries(raw, 2)):
        name, token = parts if len(parts) == 2 else ("", parts[0])
        pairs.append((name.strip() or f"token{i + 1}", token.strip()))
    return pairs


def _mcp_pairs(env: Mapping[str, str]) -> list[tuple[str, str]]:
    single = (env.get("MCP_TOKEN") or "").strip()
    head = [(DEFAULT_CLIENT, single)] if single else []
    return head + _pairs(env.get("MCP_TOKENS"))


def _room_pairs(env: Mapping[str, str]) -> list[tuple[str, str]]:
    return _pairs(env.get("ROOM_TOKENS"))


def _usable(pairs: list[tuple[str, str]]) -> dict[str, str]:
    # Повтор токена на лету = неясно, чьё имя писать в аудит: такой токен не открывает ничего.
    repeated = repeated_tokens(t for _, t in pairs)
    return {t: n for n, t in pairs if is_strong_token(t) and t not in repeated}


def _validated(pairs: list[tuple[str, str]], label: str, variables: str) -> dict[str, str]:
    repeated = repeated_tokens(t for _, t in pairs)
    if repeated:
        names = sorted({n for n, t in pairs if t in repeated})
        raise ValueError(f"{variables} lists the same token twice ({', '.join(names)}): "
                         "the client name would be ambiguous")
    weak = sorted(n for n, t in pairs if not is_strong_token(t))
    if weak:
        raise WeakTokenError(
            f"{label} tokens for {', '.join(weak)} are shorter than {MIN_TOKEN_LEN} "
            "characters; generate with `openssl rand -hex 32`")
    return {t: n for n, t in pairs}


def load_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """{токен: имя клиента}. Короткие и повторённые токены отбрасываются (при старте их
    ловит `validate_tokens`, здесь — защита на случай смены env на лету)."""
    return _usable(_mcp_pairs(os.environ if env is None else env))


def validate_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Как load_tokens, но короткий или повторённый токен — исключение с именем клиента."""
    return _validated(_mcp_pairs(os.environ if env is None else env), "MCP",
                      "MCP_TOKEN/MCP_TOKENS")


def load_room_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    return _usable(_room_pairs(os.environ if env is None else env))


def validate_room_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Короткий, повторённый или совпавший с MCP_TOKEN(S) токен — исключение."""
    source = os.environ if env is None else env
    named = _validated(_room_pairs(source), "ROOM", "ROOM_TOKENS")
    shared = sorted(named[t] for t in set(named) & {t for _, t in _mcp_pairs(source)})
    if shared:
        raise ValueError(f"ROOM_TOKENS reuse an MCP token ({', '.join(shared)}): "
                         "a room token must not open the owner's memory")
    return named


def load_office_registry(env: Mapping[str, str] | None = None) -> dict[str, OfficePrincipal]:
    """Битый OFFICE_SERVICE_TOKENS на лету = пустой реестр (все 401); при старте ловит validate."""
    try:
        return parse_registry((os.environ if env is None else env).get("OFFICE_SERVICE_TOKENS"))
    except ValueError:
        log.error("OFFICE_SERVICE_TOKENS is invalid; office intake refuses all requests")
        return {}


def validate_office_tokens(env: Mapping[str, str] | None = None) -> dict[str, OfficePrincipal]:
    """Старт: битая запись, короткий токен или токен MCP/комнаты — исключение."""
    source = os.environ if env is None else env
    registry = parse_registry(source.get("OFFICE_SERVICE_TOKENS"))
    check_disjoint(registry, {t for _, t in (*_mcp_pairs(source), *_room_pairs(source))})
    return registry


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
        if path.startswith(OFFICE_PREFIX):
            # Сервисный токен живёт только здесь; на /mcp он не совпадёт ни с одним набором.
            principal = resolve(bearer_of(scope.get("headers", [])), load_office_registry())
            if principal is None:
                await JSONResponse({"error": "unauthorized", "retryable": False},
                                   status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
            scope[OFFICE_PRINCIPAL_KEY] = principal
            scope[CLIENT_SCOPE_KEY], scope[REALM_SCOPE_KEY] = principal.name, OFFICE_REALM
            await self.app(scope, receive, send)
            return
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
