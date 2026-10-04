"""Durable, offline simulation of Gmail pagination and full-sync boundaries.

Only synthetic subjects are accepted. The HTTP adapter is an in-memory fixture;
this module has no transport, credentials, or production entrypoint.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vera_shared.ingest.gmail_pilot import (
    GmailChange,
    _account,
    _enabled,
    _idle,
    _number,
    _plain_message,
    _sub,
    capture_history_page,
    capture_present_resync,
    record_history_404,
)
from vera_shared.ingest.shadow_types import Quarantine, utc_timestamp


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _token(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise Quarantine("invalid Gmail continuation token")
    return value


def _history_manifest(response: dict[str, Any], start: str) -> list[dict[str, Any]]:
    records = response.get("history", [])
    if not isinstance(records, list):
        raise Quarantine("invalid Gmail history records")
    head = _number(str(response.get("historyId", "")))
    if head < _number(start):
        raise Quarantine("Gmail mailbox head regressed")
    manifest: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    fields = {
        "messagesAdded": "added",
        "messagesDeleted": "deleted",
        "labelsAdded": "label_added",
        "labelsRemoved": "label_removed",
    }
    for record in records:
        if not isinstance(record, dict):
            raise Quarantine("invalid Gmail history record")
        position = str(record.get("id", ""))
        if not _number(start) < _number(position) <= head:
            raise Quarantine("Gmail history record outside requested window")
        for field, kind in fields.items():
            entries = record.get(field, [])
            if not isinstance(entries, list):
                raise Quarantine("invalid Gmail history changes")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("message"), dict):
                    raise Quarantine("invalid Gmail message change")
                mid = entry["message"].get("id")
                if not isinstance(mid, str) or not mid:
                    raise Quarantine("Gmail change lacks message ID")
                labels = entry.get("labelIds", []) if field.startswith("labels") else []
                if not isinstance(labels, list) or any(
                    not isinstance(label, str) or not label for label in labels
                ):
                    raise Quarantine("invalid Gmail label IDs")
                # The API groups changes by kind, with no order within a history ID.
                # Multiple kinds for one message at that position cannot be ordered.
                key = (position, mid)
                if key in seen:
                    raise Quarantine("ambiguous same-position Gmail message changes")
                seen.add(key)
                manifest.append(
                    {
                        "history_id": position,
                        "message_id": mid,
                        "kind": kind,
                        "label_ids": sorted(set(labels)),
                    }
                )
    return manifest


async def capture_history_response(
    conn: AsyncConnection,
    sub: str,
    *,
    start_history_id: str,
    requested_token: str | None,
    response: dict[str, Any],
) -> str | None:
    """Persist one provider-shaped response; never advance the cursor mid-chain."""
    _enabled()
    _idle(conn)
    _sub(sub)
    _number(start_history_id)
    if not isinstance(response, dict):
        raise Quarantine("invalid Gmail history response")
    request = requested_token or ""
    next_token = _token(response.get("nextPageToken"))
    if next_token == request:
        raise Quarantine("Gmail continuation token loop")
    manifest = _history_manifest(response, start_history_id)
    head = str(response["historyId"])
    digest = _digest(response)
    async with conn.begin():
        account = await _account(conn, sub)
        run = (
            await conn.execute(
                text("SELECT * FROM brain_gmail_protocol_history WHERE google_sub=:sub FOR UPDATE"),
                {"sub": sub},
            )
        ).one_or_none()
        if run is None:
            if request or account.captured_cursor != start_history_id or account.resync_required:
                raise Quarantine("Gmail continuation lacks a matching starting cursor")
            await conn.execute(
                text(
                    "INSERT INTO brain_gmail_protocol_history(google_sub,start_history_id) "
                    "VALUES (:sub,:start)"
                ),
                {"sub": sub, "start": start_history_id},
            )
            expected = ""
            generation = 1
        else:
            if run.state == "complete" and run.start_history_id == start_history_id:
                prior_first = (
                    await conn.execute(
                        text(
                            "SELECT response_hash,next_token "
                            "FROM brain_gmail_protocol_history_pages "
                            "WHERE google_sub=:sub AND start_history_id=:start "
                            "AND generation=:generation AND requested_token=''"
                        ),
                        {"sub": sub, "start": start_history_id, "generation": run.generation},
                    )
                ).one_or_none()
                if not request and prior_first and prior_first.response_hash == digest:
                    return prior_first.next_token
            restart = run.state in {"complete", "expired"} and (
                run.start_history_id != start_history_id or run.state == "complete"
            )
            if restart:
                if (
                    request
                    or account.captured_cursor != start_history_id
                    or account.resync_required
                ):
                    raise Quarantine("Gmail new history chain lacks a matching cursor")
                await conn.execute(
                    text(
                        "UPDATE brain_gmail_protocol_history SET start_history_id=:start,"
                        "generation=generation+1,expected_token='',"
                        "final_history_id=NULL,state='paging' "
                        "WHERE google_sub=:sub"
                    ),
                    {"sub": sub, "start": start_history_id},
                )
                run = (
                    await conn.execute(
                        text("SELECT * FROM brain_gmail_protocol_history WHERE google_sub=:sub"),
                        {"sub": sub},
                    )
                ).one()
            generation = run.generation
            if run.start_history_id != start_history_id or run.state == "expired":
                raise Quarantine("Gmail history chain identity changed")
            old = (
                await conn.execute(
                    text(
                        "SELECT response_hash,next_token FROM brain_gmail_protocol_history_pages "
                        "WHERE google_sub=:sub AND start_history_id=:start "
                        "AND generation=:generation AND requested_token=:request"
                    ),
                    {
                        "sub": sub,
                        "start": start_history_id,
                        "generation": generation,
                        "request": request,
                    },
                )
            ).one_or_none()
            if old is not None:
                if old.response_hash != digest:
                    raise Quarantine("Gmail page changed on replay")
                return old.next_token
            expected = run.expected_token
            if run.state != "paging":
                raise Quarantine("Gmail history chain already ended")
        if request != expected or (request and request == next_token):
            raise Quarantine("Gmail continuation token mismatch")
        if next_token:
            prior = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM brain_gmail_protocol_history_pages "
                        "WHERE google_sub=:sub AND start_history_id=:start "
                        "AND generation=:generation AND requested_token=:token"
                    ),
                    {
                        "sub": sub,
                        "start": start_history_id,
                        "generation": generation,
                        "token": next_token,
                    },
                )
            ).scalar_one_or_none()
            if prior:
                raise Quarantine("Gmail continuation token cycle")
        await conn.execute(
            text(
                "INSERT INTO brain_gmail_protocol_history_pages "
                "(google_sub,start_history_id,generation,requested_token,next_token,"
                "response_history_id,response_hash,manifest) "
                "VALUES (:sub,:start,:generation,:request,:next,:head,:digest,"
                "CAST(:manifest AS jsonb))"
            ),
            {
                "sub": sub,
                "start": start_history_id,
                "generation": generation,
                "request": request,
                "next": next_token,
                "head": head,
                "digest": digest,
                "manifest": json.dumps(manifest, sort_keys=True),
            },
        )
        for change in manifest:
            event = GmailChange(change["history_id"], change["message_id"], change["kind"])
            old = (
                await conn.execute(
                    text(
                        "SELECT kind,label_ids FROM brain_gmail_protocol_obligations "
                        "WHERE google_sub=:sub AND start_history_id=:start "
                        "AND generation=:generation AND event_key=:key"
                    ),
                    {
                        "sub": sub,
                        "start": start_history_id,
                        "generation": generation,
                        "key": event.key,
                    },
                )
            ).one_or_none()
            if old is not None and (old.kind != event.kind or old.label_ids != change["label_ids"]):
                raise Quarantine("Gmail overlapping page changed an obligation")
            await conn.execute(
                text(
                    "INSERT INTO brain_gmail_protocol_obligations "
                    "(google_sub,start_history_id,generation,event_key,history_id,"
                    "message_id,kind,label_ids) "
                    "VALUES (:sub,:start,:generation,:key,:history,:mid,:kind,"
                    "CAST(:labels AS jsonb)) "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "sub": sub,
                    "start": start_history_id,
                    "generation": generation,
                    "key": event.key,
                    "history": event.history_id,
                    "mid": event.message_id,
                    "kind": event.kind,
                    "labels": json.dumps(change["label_ids"]),
                },
            )
        await conn.execute(
            text(
                "UPDATE brain_gmail_protocol_history SET expected_token=:next,"
                "final_history_id=:final,state=:state WHERE google_sub=:sub"
            ),
            {
                "sub": sub,
                "next": next_token or "",
                "final": head if not next_token else None,
                "state": "ready" if not next_token else "paging",
            },
        )
    return next_token


async def finish_history_chain(conn: AsyncConnection, sub: str) -> bool:
    """Only a persisted terminal page may turn its response head into high-water."""
    _enabled()
    _idle(conn)
    run = (
        await conn.execute(
            text(
                "SELECT start_history_id,generation,final_history_id,state FROM "
                "brain_gmail_protocol_history WHERE google_sub=:sub"
            ),
            {"sub": _sub(sub)},
        )
    ).one_or_none()
    await conn.rollback()
    if run is None or run.state not in {"ready", "complete"}:
        raise Quarantine("Gmail history chain has no terminal page")
    if run.state == "complete":
        return False
    changes = (
        await conn.execute(
            text(
                "SELECT history_id,message_id,kind FROM brain_gmail_protocol_obligations "
                "WHERE google_sub=:sub AND start_history_id=:start "
                "AND generation=:generation"
            ),
            {"sub": sub, "start": run.start_history_id, "generation": run.generation},
        )
    ).all()
    await conn.rollback()
    if _number(run.final_history_id) > _number(run.start_history_id):
        await capture_history_page(
            conn,
            sub,
            page_id=f"protocol:{run.start_history_id}:{run.generation}:{run.final_history_id}",
            start_history_id=run.start_history_id,
            end_history_id=run.final_history_id,
            changes=[GmailChange(*row) for row in changes],
        )
    elif changes:
        raise Quarantine("Gmail changes cannot have unchanged mailbox head")
    async with conn.begin():
        account = await _account(conn, sub)
        if account.captured_cursor != run.final_history_id:
            raise Quarantine("Gmail history final cursor was not committed")
        await conn.execute(
            text(
                "UPDATE brain_gmail_protocol_history SET state='complete' "
                "WHERE google_sub=:sub AND start_history_id=:start "
                "AND generation=:generation AND state='ready'"
            ),
            {"sub": sub, "start": run.start_history_id, "generation": run.generation},
        )
        await conn.execute(
            text(
                "UPDATE brain_gmail_protocol_full_sync SET state='complete' "
                "WHERE google_sub=:sub AND state='bridging' "
                "AND anchor_history_id=:start"
            ),
            {"sub": sub, "start": run.start_history_id},
        )
    return True


async def expire_history_chain(conn: AsyncConnection, sub: str) -> None:
    """Represent a provider 404 without promoting any partial page's head."""
    await record_history_404(conn, sub)
    async with conn.begin():
        await _account(conn, _sub(sub))
        await conn.execute(
            text(
                "UPDATE brain_gmail_protocol_history SET state='expired' "
                "WHERE google_sub=:sub AND state IN ('paging','ready')"
            ),
            {"sub": sub},
        )


@dataclass
class SyntheticGmailHttp:
    """Scripted HTTP response fixture; deliberately no socket or OAuth support."""

    replies: dict[tuple[str, str], tuple[int, dict[str, Any]]]

    def get(self, method: str, token: str | None) -> tuple[int, dict[str, Any]]:
        try:
            return self.replies[(method, token or "")]
        except KeyError as exc:
            raise Quarantine("synthetic Gmail HTTP response missing") from exc


async def ingest_synthetic_history_page(
    conn: AsyncConnection,
    sub: str,
    start: str,
    token: str | None,
    http: SyntheticGmailHttp,
) -> str | None:
    status, body = http.get("history.list", token)
    if status == 404:
        await expire_history_chain(conn, sub)
        return None
    if status != 200:
        raise Quarantine("synthetic Gmail history HTTP failure")
    return await capture_history_response(
        conn, sub, start_history_id=start, requested_token=token, response=body
    )


def _plain_get(raw: dict[str, Any]) -> dict[str, object]:
    """Decode only a single-part text/plain messages.get response."""
    try:
        payload = raw["payload"]
        labels = raw["labelIds"]
        millis = int(raw["internalDate"])
        encoded = payload["body"]["data"]
        if payload["mimeType"] != "text/plain" or payload.get("parts"):
            raise ValueError("unsupported MIME")
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        if not isinstance(labels, list) or any(not isinstance(x, str) for x in labels):
            raise ValueError("invalid labels")
        normalized: dict[str, object] = {
            "id": raw["id"],
            "historyId": str(raw["historyId"]),
            "internalDate": str(millis),
            "text": body,
            "labels": labels,
            "draft": "DRAFT" in labels,
            "attachments": [],
        }
        _number(str(normalized["historyId"]))
        if not _plain_message(normalized):
            raise ValueError("unsupported Gmail message")
        return normalized
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise Quarantine("unsupported or incomplete Gmail messages.get response") from exc


async def start_full_sync(
    conn: AsyncConnection,
    sub: str,
    *,
    scope: dict[str, Any],
    window_start: str,
    window_end: str,
) -> None:
    """Declare the exact list scope and time window before enumeration."""
    _enabled()
    _idle(conn)
    _sub(sub)
    start = datetime.fromisoformat(utc_timestamp(window_start))
    end = datetime.fromisoformat(utc_timestamp(window_end))
    if start >= end or set(scope) != {"labelIds", "includeSpamTrash", "q"}:
        raise Quarantine("incomplete Gmail full-sync scope or window")
    if (
        not isinstance(scope["labelIds"], list)
        or any(not isinstance(label, str) or not label for label in scope["labelIds"])
        or not isinstance(scope["includeSpamTrash"], bool)
        or not isinstance(scope["q"], str)
    ):
        raise Quarantine("invalid Gmail full-sync scope")
    # A scoped or date-limited list is not a complete present-state snapshot.
    # It must never be fed to capture_present_resync, which hides absent IDs.
    if scope != {"labelIds": [], "includeSpamTrash": True, "q": ""} or (
        start != datetime(1, 1, 1, tzinfo=UTC) or end != datetime(9999, 12, 31, tzinfo=UTC)
    ):
        raise Quarantine("present-state full sync requires whole-mailbox scope and window")
    async with conn.begin():
        account = await _account(conn, sub)
        if not account.resync_required:
            raise Quarantine("full sync requires an explicit coverage break")
        existing = (
            await conn.execute(
                text("SELECT 1 FROM brain_gmail_protocol_full_sync WHERE google_sub=:sub"),
                {"sub": sub},
            )
        ).scalar_one_or_none()
        if existing:
            raise Quarantine("full sync already exists; resolve it explicitly")
        await conn.execute(
            text(
                "INSERT INTO brain_gmail_protocol_full_sync "
                "(google_sub,scope,window_start,window_end) "
                "VALUES (:sub,CAST(:scope AS jsonb),:start,:end)"
            ),
            {"sub": sub, "scope": json.dumps(scope, sort_keys=True), "start": start, "end": end},
        )


async def capture_full_sync_response(
    conn: AsyncConnection,
    sub: str,
    *,
    requested_token: str | None,
    response: dict[str, Any],
    fetched: dict[str, dict[str, Any]],
) -> str | None:
    """Persist one messages.list page and its exact messages.get receipts."""
    _enabled()
    _idle(conn)
    _sub(sub)
    if not isinstance(response, dict) or not isinstance(response.get("messages", []), list):
        raise Quarantine("invalid Gmail messages.list response")
    entries = response.get("messages", [])
    ids: list[str] = []
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise Quarantine("invalid Gmail listed message")
        ids.append(item["id"])
    if len(ids) != len(set(ids)) or set(fetched) != set(ids):
        raise Quarantine("Gmail list/get enumeration is incomplete or duplicated")
    normalized = {mid: _plain_get(fetched[mid]) for mid in ids}
    if any(normalized[mid]["id"] != mid for mid in ids):
        raise Quarantine("Gmail messages.get identity differs from list ID")
    request = requested_token or ""
    next_token = _token(response.get("nextPageToken"))
    if next_token == request:
        raise Quarantine("Gmail full-sync continuation token loop")
    digest = _digest({"list": response, "get": fetched})
    async with conn.begin():
        await _account(conn, sub)
        run = (
            await conn.execute(
                text(
                    "SELECT * FROM brain_gmail_protocol_full_sync WHERE google_sub=:sub FOR UPDATE"
                ),
                {"sub": sub},
            )
        ).one_or_none()
        if run is None:
            raise Quarantine("Gmail full sync was not declared")
        old = (
            await conn.execute(
                text(
                    "SELECT response_hash,next_token FROM brain_gmail_protocol_full_pages "
                    "WHERE google_sub=:sub AND requested_token=:token"
                ),
                {"sub": sub, "token": request},
            )
        ).one_or_none()
        if old is not None:
            if old.response_hash != digest:
                raise Quarantine("Gmail full-sync page changed on replay")
            return old.next_token
        if run.state != "paging" or request != run.expected_token:
            raise Quarantine("Gmail full-sync continuation mismatch")
        anchor = run.anchor_history_id
        if anchor is None:
            if not ids:
                raise Quarantine("empty first page has no full-sync history anchor")
            anchor = str(normalized[ids[0]]["historyId"])
        for mid, payload in normalized.items():
            created = datetime.fromtimestamp(int(str(payload["internalDate"])) / 1000, UTC)
            start = (
                run.window_start.replace(tzinfo=UTC)
                if run.window_start.tzinfo is None
                else run.window_start.astimezone(UTC)
            )
            end = (
                run.window_end.replace(tzinfo=UTC)
                if run.window_end.tzinfo is None
                else run.window_end.astimezone(UTC)
            )
            if not start <= created < end:
                raise Quarantine("Gmail item lies outside declared full-sync window")
            if _number(str(payload["historyId"])) > _number(anchor):
                raise Quarantine("Gmail enumeration raced beyond its history anchor")
            prior = (
                await conn.execute(
                    text(
                        "SELECT payload FROM brain_gmail_protocol_full_items "
                        "WHERE google_sub=:sub AND message_id=:mid"
                    ),
                    {"sub": sub, "mid": mid},
                )
            ).scalar_one_or_none()
            if prior is not None and prior != payload:
                raise Quarantine("Gmail overlapping full-sync message changed")
        await conn.execute(
            text(
                "INSERT INTO brain_gmail_protocol_full_pages "
                "(google_sub,requested_token,next_token,response_hash,manifest) "
                "VALUES (:sub,:request,:next,:digest,CAST(:manifest AS jsonb))"
            ),
            {
                "sub": sub,
                "request": request,
                "next": next_token,
                "digest": digest,
                "manifest": json.dumps(ids),
            },
        )
        for mid, payload in normalized.items():
            await conn.execute(
                text(
                    "INSERT INTO brain_gmail_protocol_full_items "
                    "(google_sub,message_id,payload) "
                    "VALUES (:sub,:mid,CAST(:payload AS jsonb)) ON CONFLICT DO NOTHING"
                ),
                {"sub": sub, "mid": mid, "payload": json.dumps(payload, sort_keys=True)},
            )
        await conn.execute(
            text(
                "UPDATE brain_gmail_protocol_full_sync SET anchor_history_id=:anchor,"
                "expected_token=:next,state=:state WHERE google_sub=:sub"
            ),
            {
                "sub": sub,
                "anchor": anchor,
                "next": next_token or "",
                "state": "paging" if next_token else "ready",
            },
        )
    return next_token


async def ingest_synthetic_full_page(
    conn: AsyncConnection, sub: str, token: str | None, http: SyntheticGmailHttp
) -> str | None:
    """Fetch a scripted list page and all GETs before its durable page commit."""
    status, response = http.get("messages.list", token)
    if status != 200 or not isinstance(response.get("messages", []), list):
        raise Quarantine("synthetic Gmail messages.list failed")
    fetched: dict[str, dict[str, Any]] = {}
    for item in response.get("messages", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise Quarantine("invalid Gmail listed message")
        mid = item["id"]
        get_status, body = http.get("messages.get", mid)
        if get_status != 200:
            raise Quarantine("synthetic Gmail messages.get failed")
        fetched[mid] = body
    return await capture_full_sync_response(
        conn, sub, requested_token=token, response=response, fetched=fetched
    )


async def finish_full_sync(conn: AsyncConnection, sub: str, observed_at: str) -> str:
    """Publish an enumerated snapshot, then require a history bridge from its anchor."""
    _enabled()
    _idle(conn)
    run = (
        await conn.execute(
            text(
                "SELECT anchor_history_id,state FROM brain_gmail_protocol_full_sync "
                "WHERE google_sub=:sub"
            ),
            {"sub": _sub(sub)},
        )
    ).one_or_none()
    await conn.rollback()
    if run is None or run.state not in {"ready", "bridging", "complete"}:
        raise Quarantine("Gmail full-sync enumeration is incomplete")
    if run.state != "ready":
        return run.anchor_history_id
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT payload FROM brain_gmail_protocol_full_items "
                    "WHERE google_sub=:sub ORDER BY message_id"
                ),
                {"sub": sub},
            )
        )
        .scalars()
        .all()
    )
    await conn.rollback()
    already_published = (
        await conn.execute(
            text(
                "SELECT 1 FROM brain_gmail_pilot_resyncs "
                "WHERE google_sub=:sub AND history_id=:anchor"
            ),
            {"sub": sub, "anchor": run.anchor_history_id},
        )
    ).scalar_one_or_none()
    await conn.rollback()
    if not already_published:
        await capture_present_resync(
            conn, sub, run.anchor_history_id, [dict(row) for row in rows], observed_at
        )
    async with conn.begin():
        await _account(conn, sub)
        bridge_complete = (
            await conn.execute(
                text(
                    "SELECT 1 FROM brain_gmail_protocol_history "
                    "WHERE google_sub=:sub AND start_history_id=:anchor "
                    "AND state='complete'"
                ),
                {"sub": sub, "anchor": run.anchor_history_id},
            )
        ).scalar_one_or_none()
        await conn.execute(
            text(
                "UPDATE brain_gmail_protocol_full_sync SET state=:state "
                "WHERE google_sub=:sub AND state='ready'"
            ),
            {"sub": sub, "state": "complete" if bridge_complete else "bridging"},
        )
    return run.anchor_history_id


async def full_sync_state(conn: AsyncConnection, sub: str) -> str | None:
    state = (
        await conn.execute(
            text("SELECT state FROM brain_gmail_protocol_full_sync WHERE google_sub=:sub"),
            {"sub": _sub(sub)},
        )
    ).scalar_one_or_none()
    await conn.rollback()
    return state
