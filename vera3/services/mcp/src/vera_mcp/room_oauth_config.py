"""Room OAuth configuration: env flags, approved actors and redirect hosts,
AuthSettings and the DCR size limits. Read by `room_oauth` and the room tools."""
from __future__ import annotations

import os

from mcp.server.auth.provider import OAuthClientInformationFull, RegistrationError
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from pydantic import AnyHttpUrl

from vera_mcp.auth import load_room_tokens, load_tokens

PUBLIC_ORIGIN = "https://dima.veranda.my"
RESOURCE = f"{PUBLIC_ORIGIN}/mcp"
SCOPES = ["room:read", "room:write"]
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
CODE_SECONDS = 300
PENDING_SECONDS = 600
INTERNAL_AGENTS = ("vera", "watchdog")


def enabled() -> bool:
    return os.environ.get("ROOM_OAUTH_ENABLED") == "1"


def allowed_actors() -> frozenset[str]:
    # New identities require a separately approved config change.
    return frozenset(filter(None, (x.strip() for x in
                     os.environ.get("ROOM_OAUTH_ACTORS", "dot").split(","))))


def configured_agents(include_internal: bool = True) -> frozenset[str]:
    """Имена сконфигурированных агентов; внутренние авторы vera и watchdog — по флагу."""
    names = set(load_room_tokens().values()) | set(load_tokens().values())
    names |= set(allowed_actors())
    return frozenset(names | set(INTERNAL_AGENTS) if include_internal else names)


def allowed_redirect_hosts() -> frozenset[str]:
    return frozenset(filter(None, (x.strip().lower() for x in
                     os.environ.get("ROOM_OAUTH_REDIRECT_HOSTS", "chatgpt.com").split(","))))


def validate_config() -> None:
    if not enabled():
        return
    if len(os.environ.get("ROOM_OAUTH_KEY", "")) < 32:
        raise ValueError("room OAuth requires a dedicated ROOM_OAUTH_KEY (32+ chars)")
    if not allowed_actors():
        raise ValueError("room OAuth requires at least one approved actor")
    if not allowed_redirect_hosts():
        raise ValueError("room OAuth requires an approved redirect host")
    # Автор в комнате — имя токена. OAuth-actor с именем статического агента писал
    # бы как он, и история, курсоры и аренды перестали бы их различать.
    static = set(load_room_tokens().values()) | set(load_tokens().values())
    if clash := sorted(allowed_actors() & static):
        raise ValueError(f"ROOM_OAUTH_ACTORS {', '.join(clash)} already used by a static token")


def settings() -> AuthSettings:
    return AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_ORIGIN),
        resource_server_url=AnyHttpUrl(RESOURCE),
        validate_token_resource=True,
        required_scopes=SCOPES,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES),
        revocation_options=RevocationOptions(enabled=True),
    )


# /register анонимный: без потолков один IP мог бы наполнять общий Postgres
# многокилобайтными клиентами (ревью безопасности 08.10.2026, F3).
MAX_CLIENT_NAME = 200
MAX_REDIRECT_URIS = 5
MAX_REDIRECT_URI_LEN = 500
MAX_CLIENT_METADATA_BYTES = 4096


def check_client_size(client_info: OAuthClientInformationFull) -> None:
    uris = client_info.redirect_uris or []
    if (len(client_info.client_name or "") > MAX_CLIENT_NAME
            or len(uris) > MAX_REDIRECT_URIS
            or any(len(str(uri)) > MAX_REDIRECT_URI_LEN for uri in uris)
            or len(client_info.model_dump_json().encode()) > MAX_CLIENT_METADATA_BYTES):
        raise RegistrationError("invalid_client_metadata", "Client metadata is too large")
