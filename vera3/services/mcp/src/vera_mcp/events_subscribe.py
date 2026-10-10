"""events/subscribe и events/unsubscribe: ACL по принципалу, SSRF-охрана, подписанный challenge."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import update
from vera_shared.crypto import encrypt
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import EVENT_KINDS
from vera_shared.db.models_room_events import (
    RoomEventOutboxRow,
    RoomEventSubscriptionRow,
)
from vera_shared.timeutil import utc_naive_now

from vera_mcp.auth import allowed_rooms, client_of
from vera_mcp.events_catalog import TASK_KINDS
from vera_mcp.events_net import (
    CallbackRejected,
    check_callback_url,
    http_client,
    signed_headers,
    validate_secret,
)

CALLBACK_ENDPOINT_ERROR = -32015
INVALID_PARAMS = -32602
DEFAULT_TTL_MS = 24 * 3600 * 1000
MAX_TTL_MS = 30 * 24 * 3600 * 1000


class Delivery(BaseModel):
    mode: str
    url: str
    secret: str | None = None


class SubscribeParams(types.RequestParams):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    delivery: Delivery
    cursor: str | None = None
    ttl_ms: int | None = Field(default=DEFAULT_TTL_MS, alias="ttlMs")


class UnsubscribeParams(types.RequestParams):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    delivery: Delivery


class RoomArguments(BaseModel):
    model_config = {"extra": "forbid"}
    room: str
    task_id: str | None = None
    kind: list[str] = Field(default_factory=list)


def principal_of(ctx: ServerRequestContext) -> str:
    return client_of(SimpleNamespace(request_context=ctx))


def _invalid(reason: str) -> MCPError:
    return MCPError(INVALID_PARAMS, "Invalid params", {"reason": reason})


def canonical_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        args = RoomArguments.model_validate(raw)
    except ValidationError as exc:
        raise _invalid("bad_arguments") from exc
    if args.room not in allowed_rooms():
        raise _invalid("room_not_allowed")
    if any(k not in EVENT_KINDS for k in args.kind):
        raise _invalid("unknown_kind")
    return {"room": args.room, "task_id": args.task_id, "kind": sorted(set(args.kind))}


def subscription_id(principal: str, url: str, name: str, args: dict[str, Any]) -> str:
    canon = json.dumps([principal, url, name, args], sort_keys=True, separators=(",", ":"))
    return "sub_" + hashlib.sha256(canon.encode()).hexdigest()[:40]


def _check_name(name: str) -> None:
    if name not in TASK_KINDS:
        raise _invalid("unknown_event")


async def _challenge(sub_id: str, url: str, secret: str) -> None:
    token = secrets.token_urlsafe(24)
    body = json.dumps({"type": "verification", "challenge": token}, separators=(",", ":"))
    headers = signed_headers(secret, f"vrf_{uuid.uuid4().hex}", body, sub_id)
    try:
        async with http_client(timeout=5.0) as client:
            resp = await client.post(url, content=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise MCPError(CALLBACK_ENDPOINT_ERROR, "CallbackEndpointError", {"reason": "timeout"}) from exc
    except httpx.HTTPError as exc:
        raise MCPError(CALLBACK_ENDPOINT_ERROR, "CallbackEndpointError", {"reason": "unreachable"}) from exc
    echoed = ""
    if resp.is_success:
        try:
            echoed = str(resp.json().get("challenge", ""))
        except (ValueError, AttributeError):
            echoed = ""
    if not echoed or not hmac.compare_digest(echoed.encode(), token.encode()):
        raise MCPError(CALLBACK_ENDPOINT_ERROR, "CallbackEndpointError", {"reason": "challenge_failed"})


def _expiry(ttl_ms: int | None):
    if ttl_ms is None:
        return None
    return utc_naive_now() + timedelta(milliseconds=min(max(ttl_ms, 1), MAX_TTL_MS))


async def subscribe(ctx: ServerRequestContext, params: SubscribeParams) -> dict[str, Any]:
    principal = principal_of(ctx)
    _check_name(params.name)
    d = params.delivery
    if d.mode != "webhook" or not d.secret:
        raise _invalid("webhook_secret_required")
    args = canonical_arguments(params.arguments)
    try:
        validate_secret(d.secret)
        await check_callback_url(d.url)
    except CallbackRejected as exc:
        raise _invalid(exc.reason) from exc
    sub_id = subscription_id(principal, d.url, params.name, args)
    await _challenge(sub_id, d.url, d.secret)
    expires = _expiry(params.ttl_ms)
    values = {"principal": principal, "room": args["room"], "event_name": params.name,
              "task_id": args["task_id"], "kinds": args["kind"], "callback_url": d.url,
              "encrypted_secret": encrypt(d.secret, secret=os.environ["ROOM_OAUTH_KEY"]),
              "status": "active", "expires_at": expires}
    async with get_session() as s:
        row = await s.get(RoomEventSubscriptionRow, sub_id)
        if row is None:
            s.add(RoomEventSubscriptionRow(id=sub_id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
    refresh = expires.isoformat(timespec="seconds") + "Z" if expires else None
    return {"resultType": "complete", "id": sub_id, "refreshBefore": refresh,
            "cursor": None, "truncated": False}


async def revoke_subscription(sub_id: str) -> None:
    """Отзыв гасит и ещё не доставленные строки outbox."""
    async with get_session() as s:
        await s.execute(update(RoomEventSubscriptionRow)
                        .where(RoomEventSubscriptionRow.id == sub_id).values(status="revoked"))
        await s.execute(update(RoomEventOutboxRow)
                        .where(RoomEventOutboxRow.subscription_id == sub_id,
                               RoomEventOutboxRow.status == "pending").values(status="revoked"))


async def unsubscribe(ctx: ServerRequestContext, params: UnsubscribeParams) -> dict[str, Any]:
    principal = principal_of(ctx)
    _check_name(params.name)
    args = canonical_arguments(params.arguments)
    await revoke_subscription(subscription_id(principal, params.delivery.url, params.name, args))
    return {"resultType": "complete"}
