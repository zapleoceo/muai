"""Opt-in OAuth authorization server for the *room* MCP realm only.

No OAuth route or token is enabled until ROOM_OAUTH_ENABLED=1. The dashboard
handles human consent; this MCP service never receives its owner-cookie key.
Opaque bearer values are hashed in Postgres. DCR client metadata uses a
dedicated ROOM_OAUTH_KEY.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
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
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from pydantic import AnyHttpUrl
from sqlalchemy import delete
from vera_shared.crypto import decrypt, encrypt
from vera_shared.db.engine import get_session

from vera_mcp.auth import load_room_tokens, match_token
from vera_mcp.oauth_models import RoomOAuthClient, RoomOAuthGrant

PUBLIC_ORIGIN = "https://dima.veranda.my"
RESOURCE = f"{PUBLIC_ORIGIN}/mcp"
log = logging.getLogger(__name__)
SCOPES = ["room:read", "room:write"]
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
CODE_SECONDS = 300
PENDING_SECONDS = 600


def enabled() -> bool:
    return os.environ.get("ROOM_OAUTH_ENABLED") == "1"


def allowed_actors() -> frozenset[str]:
    # New identities require a separately approved config change.
    return frozenset(filter(None, (x.strip() for x in
                     os.environ.get("ROOM_OAUTH_ACTORS", "dot").split(","))))


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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _future(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _alive(row: RoomOAuthGrant | None) -> bool:
    if row is None:
        return False
    dt = row.expires_at
    if dt.tzinfo is None:  # SQLite in unit tests
        dt = dt.replace(tzinfo=UTC)
    return dt > datetime.now(UTC)


class RoomOAuthProvider:
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
                token_hash=_digest(ticket), kind="pending", client_id=client.client_id,
                data_json=json.dumps(data), expires_at=_future(PENDING_SECONDS),
            ))
        return f"{PUBLIC_ORIGIN}/room/oauth/consent?{urlencode({'ticket': ticket})}"

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, _digest(authorization_code))
            if not _alive(row) or row.kind != "code" or row.client_id != client.client_id:
                return None
            return AuthorizationCode.model_validate({"code": authorization_code,
                                                      **json.loads(row.data_json)})

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, _digest(authorization_code.code),
                               with_for_update=True)
            if not _alive(row) or row.kind != "code" or row.client_id != client.client_id:
                raise TokenError("invalid_grant")
            await db.delete(row)
            await db.flush()
            return await self._issue(db, client.client_id, row.actor, authorization_code.scopes,
                                     authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, _digest(refresh_token))
            if not _alive(row) or row.kind != "refresh" or row.client_id != client.client_id:
                return None
            return RefreshToken(token=refresh_token, client_id=row.client_id,
                                scopes=json.loads(row.data_json)["scopes"],
                                expires_at=int(row.expires_at.replace(tzinfo=UTC).timestamp()),
                                resource=RESOURCE, subject=row.actor)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, _digest(refresh_token.token),
                               with_for_update=True)
            if not _alive(row) or row.kind != "refresh" or row.client_id != client.client_id:
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
            row = await db.get(RoomOAuthGrant, _digest(token))
            if not _alive(row) or row.kind != "access" or row.actor not in allowed_actors():
                return None
            return AccessToken(token=token, client_id=row.client_id,
                               scopes=json.loads(row.data_json)["scopes"],
                               expires_at=int(row.expires_at.replace(tzinfo=UTC).timestamp()),
                               resource=RESOURCE, subject=row.actor)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, _digest(token.token))
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
            db.add(RoomOAuthGrant(token_hash=_digest(token), kind=kind,
                                  client_id=client_id, actor=actor,
                                  data_json=json.dumps({"scopes": scopes}),
                                  expires_at=_future(ttl), family_id=family))
        log.info("room OAuth grant issued: client=%r actor=%r scopes=%s",
                 client_id, actor, " ".join(scopes))
        return OAuthToken(access_token=access, expires_in=ACCESS_SECONDS,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def pending_details(self, ticket: str) -> dict | None:
        """Internal dashboard call only; returns display metadata, never secrets."""
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, _digest(ticket)) if ticket else None
            if not _alive(row) or row.kind != "pending":
                return None
            client = await db.get(RoomOAuthClient, row.client_id)
            if client is None:
                return None
            info = OAuthClientInformationFull.model_validate_json(
                decrypt(client.encrypted_info, secret=os.environ["ROOM_OAUTH_KEY"]))
            params = AuthorizationParams.model_validate_json(row.data_json)
            return {"client_id": row.client_id, "client_name": info.client_name,
                    "redirect_uri": str(params.redirect_uri),
                    "scopes": params.scopes or SCOPES,
                    "actors": sorted(allowed_actors())}

    async def approve_pending(self, ticket: str, actor: str) -> str | None:
        """Internal dashboard call only; consumes a ticket once and returns redirect."""
        if actor not in allowed_actors():
            return None
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, _digest(ticket), with_for_update=True)
            if not _alive(row) or row.kind != "pending":
                return None
            params = AuthorizationParams.model_validate_json(row.data_json)
            code = secrets.token_urlsafe(32)
            code_data = {
                "scopes": params.scopes or SCOPES,
                "expires_at": _future(CODE_SECONDS).timestamp(),
                "client_id": row.client_id,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource, "subject": actor,
            }
            db.add(RoomOAuthGrant(token_hash=_digest(code), kind="code",
                                  client_id=row.client_id, actor=actor,
                                  data_json=json.dumps(code_data),
                                  expires_at=_future(CODE_SECONDS)))
            await db.delete(row)
            log.info("room OAuth consent approved: client=%r actor=%r", row.client_id, actor)
        query = {"code": code}
        if params.state:
            query["state"] = params.state
        sep = "&" if params.redirect_uri.query else "?"
        return f"{params.redirect_uri}{sep}{urlencode(query)}"
