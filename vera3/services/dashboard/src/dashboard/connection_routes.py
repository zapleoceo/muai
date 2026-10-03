"""Правка связей из карточки человека: разорвать роль, отвергнуть выведенную, вернуть.

Только владелец, только со страницы дашборда (`csrf.same_origin_or_403`). Логика —
в `vera_shared.graph.connection_actions` и `vera_shared.journal.undo`, те же, что
у MCP; здесь лишь разбор запроса и перевод ошибок в HTTP.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from vera_shared.db.engine import get_session
from vera_shared.graph.connection_actions import break_role, reject_inferred
from vera_shared.graph.edit import GraphEditError
from vera_shared.journal.audit import AuditNotFound
from vera_shared.journal.undo import UndoRefused, undo_entry

from dashboard.csrf import same_origin_or_403
from dashboard.render import owner_or_blank_401

router = APIRouter()
CLIENT = "dashboard"
EntityId = Annotated[int, Field(ge=1)]


class BreakRole(BaseModel):
    entity_a: EntityId
    entity_b: EntityId
    predicate: Annotated[str, Field(min_length=1, max_length=80)]
    rel_ids: Annotated[list[EntityId], Field(min_length=1, max_length=20)]


class Pair(BaseModel):
    entity_a: EntityId
    entity_b: EntityId


class UndoRequest(BaseModel):
    audit_ids: Annotated[list[EntityId], Field(min_length=1, max_length=20)]


def _gate(request: Request) -> JSONResponse | None:
    if owner_or_blank_401(request) is not None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return same_origin_or_403(request)


@router.post("/api/graph/connection/break", response_class=JSONResponse)
async def break_connection(request: Request, body: BreakRole):
    if (denied := _gate(request)) is not None:
        return denied
    try:
        audit_ids = await break_role(body.entity_a, body.entity_b, body.predicate, body.rel_ids, CLIENT)
    except GraphEditError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return JSONResponse({"ok": True, "audit_ids": audit_ids})


@router.post("/api/graph/connection/reject", response_class=JSONResponse)
async def reject_connection(request: Request, body: Pair):
    if (denied := _gate(request)) is not None:
        return denied
    try:
        audit_id = await reject_inferred(body.entity_a, body.entity_b, CLIENT)
    except GraphEditError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return JSONResponse({"ok": True, "audit_ids": [audit_id] if audit_id else []})


@router.post("/api/journal/undo", response_class=JSONResponse)
async def undo_edits(request: Request, body: UndoRequest):
    if (denied := _gate(request)) is not None:
        return denied
    undone: list[int] = []
    try:
        for audit_id in body.audit_ids:
            async with get_session() as s:
                await undo_entry(s, audit_id, CLIENT, force=False)
            undone.append(audit_id)
    except (UndoRefused, AuditNotFound, GraphEditError) as e:
        return JSONResponse({"error": str(e), "undone": undone}, status_code=409)
    return JSONResponse({"ok": True, "undone": undone})
