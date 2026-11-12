"""Opt-in OAuth authorization server for the *room* MCP realm only.

No OAuth route or token is enabled until ROOM_OAUTH_ENABLED=1. The dashboard
handles human consent; this MCP service never receives its owner-cookie key.
Opaque bearer values are hashed in Postgres. DCR client metadata uses a
dedicated ROOM_OAUTH_KEY.

Config and DCR limits live in `room_oauth_config`, grant primitives in
`room_oauth_grant`, the dashboard consent steps in `room_oauth_consent`;
every public name is re-exported here.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime
from urllib.parse import urlencode, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from sqlalchemy import delete
from vera_shared.crypto import decrypt, encrypt
from vera_shared.db.engine import get_session

from vera_mcp.auth import load_room_tokens, match_token
from vera_mcp.oauth_models import RoomOAuthClient, RoomOAuthGrant
from vera_mcp.room_oauth_config import (
    ACCESS_SECONDS,
    CODE_SECONDS,
    INTERNAL_AGENTS,
    MAX_CLIENT_METADATA_BYTES,
    MAX_CLIENT_NAME,
    MAX_REDIRECT_URI_LEN,
    MAX_REDIRECT_URIS,
    PENDING_SECONDS,
    PUBLIC_ORIGIN,
    REFRESH_SECONDS,
    RESOURCE,
    SCOPES,
    allowed_actors,
    allowed_redirect_hosts,
    check_client_size,
    configured_agents,
    enabled,
    settings,
    validate_config,
)
from vera_mcp.room_oauth_consent import RoomOAuthConsentMixin
from vera_mcp.room_oauth_grant import expiry_after, grant_alive, grant_hash

__all__ = [
    "ACCESS_SECONDS", "CODE_SECONDS", "INTERNAL_AGENTS", "MAX_CLIENT_METADATA_BYTES",
    "MAX_CLIENT_NAME", "MAX_REDIRECT_URIS", "MAX_REDIRECT_URI_LEN", "PENDING_SECONDS",
    "PUBLIC_ORIGIN", "REFRESH_SECONDS", "RESOURCE", "SCOPES", "RoomOAuthProvider",
    "allowed_actors", "allowed_redirect_hosts", "configured_agents", "enabled",
    "settings", "validate_config",
]

log = logging.getLogger(__name__)


class RoomOAuthProvider(RoomOAuthConsentMixin):
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with get_session() as db:
            row = await db.get(RoomOAuthClient, client_id)
            if row is None:
                return None
            return OAuthClientInformationFull.model_validate_json(
                decrypt(row.encrypted_info, secret=os.environ["ROOM_OAUTH_KEY"]))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id or not client_info.client_secret:
            raise RegistrationError("invalid_client_metadata", "Confidential client required")
        check_client_size(client_info)
        if not client_info.redirect_uris or any(
                (parsed := urlparse(str(uri))).scheme != "https"
                or parsed.hostname not in allowed_redirect_hosts()
                or parsed.username or parsed.password or parsed.fragment
                for uri in client_info.redirect_uris):
            raise RegistrationError("invalid_redirect_uri", "Redirect host is not approved")
        async with get_session() as db:
            db.add(RoomOAuthClient(
                client_id=client_info.client_id,
                encrypted_info=encrypt(client_info.model_dump_json(),
                                       secret=os.environ["ROOM_OAUTH_KEY"]),
            ))

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        if params.resource not in (None, RESOURCE) or set(params.scopes or SCOPES) != set(SCOPES):
            raise AuthorizeError("invalid_scope", "Only the room resource and scopes are available")
        ticket = secrets.token_urlsafe(32)
        data = params.model_copy(update={"resource": RESOURCE}).model_dump(mode="json")
        async with get_session() as db:
            await db.execute(delete(RoomOAuthGrant).where(
                RoomOAuthGrant.expires_at < datetime.now(UTC)))
            db.add(RoomOAuthGrant(
                token_hash=grant_hash(ticket), kind="pending", client_id=client.client_id,
                data_json=json.dumps(data), expires_at=expiry_after(PENDING_SECONDS),
            ))
        return f"{PUBLIC_ORIGIN}/room/oauth/consent?{urlencode({'ticket': ticket})}"

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, grant_hash(authorization_code))
            if not grant_alive(row) or row.kind != "code" or row.client_id != client.client_id:
                return None
            return AuthorizationCode.model_validate({"code": authorization_code,
                                                      **json.loads(row.data_json)})

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, grant_hash(authorization_code.code),
                               with_for_update=True)
            if not grant_alive(row) or row.kind != "code" or row.client_id != client.client_id:
                raise TokenError("invalid_grant")
            await db.delete(row)
            await db.flush()
            return await self._issue(db, client.client_id, row.actor, authorization_code.scopes,
                                     authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, grant_hash(refresh_token))
            if not grant_alive(row) or row.kind != "refresh" or row.client_id != client.client_id:
                return None
            return RefreshToken(token=refresh_token, client_id=row.client_id,
                                scopes=json.loads(row.data_json)["scopes"],
                                expires_at=int(row.expires_at.replace(tzinfo=UTC).timestamp()),
                                resource=RESOURCE, subject=row.actor)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, grant_hash(refresh_token.token),
                               with_for_update=True)
            if not grant_alive(row) or row.kind != "refresh" or row.client_id != client.client_id:
                raise TokenError("invalid_grant")
            original = json.loads(row.data_json)["scopes"]
            requested = scopes or original
            if not set(requested).issubset(set(original)):
                raise TokenError("invalid_scope")
            # Revoke the old family, including an access token still in flight.
            await db.execute(delete(RoomOAuthGrant).where(
                RoomOAuthGrant.family_id == row.family_id))
            await db.flush()
            return await self._issue(db, client.client_id, row.actor, requested, RESOURCE)

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Existing room-only keys remain valid while clients migrate.
        legacy_actor = match_token(token, load_room_tokens())
        if legacy_actor:
            return AccessToken(token=token, client_id="legacy-room", scopes=SCOPES,
                               resource=RESOURCE, subject=legacy_actor)
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, grant_hash(token))
            if not grant_alive(row) or row.kind != "access" or row.actor not in allowed_actors():
                return None
            return AccessToken(token=token, client_id=row.client_id,
                               scopes=json.loads(row.data_json)["scopes"],
                               expires_at=int(row.expires_at.replace(tzinfo=UTC).timestamp()),
                               resource=RESOURCE, subject=row.actor)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, grant_hash(token.token))
            if row and row.family_id:
                await db.execute(delete(RoomOAuthGrant).where(
                    RoomOAuthGrant.family_id == row.family_id))
                log.info("room OAuth grant revoked: client=%r actor=%r",
                         row.client_id, row.actor)

    async def _issue(self, db, client_id: str, actor: str, scopes: list[str],
                     resource: str | None) -> OAuthToken:
        if actor not in allowed_actors() or resource != RESOURCE:
            raise TokenError("invalid_grant")
        await db.execute(delete(RoomOAuthGrant).where(
            RoomOAuthGrant.expires_at < datetime.now(UTC)))
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        family = uuid.uuid4().hex
        for token, kind, ttl in ((access, "access", ACCESS_SECONDS),
                                 (refresh, "refresh", REFRESH_SECONDS)):
            db.add(RoomOAuthGrant(token_hash=grant_hash(token), kind=kind,
                                  client_id=client_id, actor=actor,
                                  data_json=json.dumps({"scopes": scopes}),
                                  expires_at=expiry_after(ttl), family_id=family))
        log.info("room OAuth grant issued: client=%r actor=%r scopes=%s",
                 client_id, actor, " ".join(scopes))
        return OAuthToken(access_token=access, expires_in=ACCESS_SECONDS,
                          refresh_token=refresh, scope=" ".join(scopes))
