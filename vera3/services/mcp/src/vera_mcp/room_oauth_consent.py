"""Room OAuth consent steps called by the dashboard over the internal endpoint."""
from __future__ import annotations

import json
import logging
import os
import secrets
from urllib.parse import urlencode

from mcp.server.auth.provider import AuthorizationParams, OAuthClientInformationFull
from vera_shared.crypto import decrypt
from vera_shared.db.engine import get_session

from vera_mcp.oauth_models import RoomOAuthClient, RoomOAuthGrant
from vera_mcp.room_oauth_config import CODE_SECONDS, SCOPES, allowed_actors
from vera_mcp.room_oauth_grant import expiry_after, grant_alive, grant_hash

log = logging.getLogger("vera_mcp.room_oauth")


class RoomOAuthConsentMixin:
    async def pending_details(self, ticket: str) -> dict | None:
        """Internal dashboard call only; returns display metadata, never secrets."""
        async with get_session() as db:
            row = await db.get(RoomOAuthGrant, grant_hash(ticket)) if ticket else None
            if not grant_alive(row) or row.kind != "pending":
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
            row = await db.get(RoomOAuthGrant, grant_hash(ticket), with_for_update=True)
            if not grant_alive(row) or row.kind != "pending":
                return None
            params = AuthorizationParams.model_validate_json(row.data_json)
            code = secrets.token_urlsafe(32)
            code_data = {
                "scopes": params.scopes or SCOPES,
                "expires_at": expiry_after(CODE_SECONDS).timestamp(),
                "client_id": row.client_id,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource, "subject": actor,
            }
            db.add(RoomOAuthGrant(token_hash=grant_hash(code), kind="code",
                                  client_id=row.client_id, actor=actor,
                                  data_json=json.dumps(code_data),
                                  expires_at=expiry_after(CODE_SECONDS)))
            await db.delete(row)
            log.info("room OAuth consent approved: client=%r actor=%r", row.client_id, actor)
        query = {"code": code}
        if params.state:
            query["state"] = params.state
        sep = "&" if params.redirect_uri.query else "?"
        return f"{params.redirect_uri}{sep}{urlencode(query)}"
