"""Объединение людей из карточки: поиск, предпросмотр, слияние, перенос связей.

Все ручки — только владелец; POST дополнительно со страницы дашборда (`csrf.same_origin_or_403`).
Предпросмотр ничего не пишет (слияние откатывается). Логика — в `vera_shared.graph`:
`merge_actions` (тот же путь и журнал, что у MCP `entity_merge`), `relationship_move`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from vera_shared.graph.edit import GraphEditError
from vera_shared.graph.merge_actions import apply_merge, preview_merge
from vera_shared.graph.merge_candidates import entity_summaries, recommend_keep
from vera_shared.graph.merge_errors import MergeError
from vera_shared.graph.merge_guard import MergeBlocked
from vera_shared.graph.relationship_move import (
    move_relationships,
    name_evidence,
    preview_move,
)
from vera_shared.graph.search import search_entities

from dashboard.connection_routes import CLIENT, EntityId
from dashboard.csrf import owner_gate
from dashboard.csrf import owner_post_gate as _gate

router = APIRouter()
SEARCH_SHOWN = 8
MIN_QUERY = 2
REASON = "dashboard: объединено владельцем из карточки"


class MergePair(BaseModel):
    a: EntityId           # карточка, из которой начали
    b: EntityId           # найденный человек
    keep_id: EntityId | None = None


class MergeApply(BaseModel):
    keep_id: EntityId
    drop_id: EntityId


class MovePair(BaseModel):
    from_id: EntityId
    to_id: EntityId


class MoveApply(MovePair):
    rel_ids: Annotated[list[EntityId], Field(min_length=1, max_length=200)]


def _error(e: Exception) -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=409)


@router.get("/api/graph/people/search", response_class=JSONResponse)
async def search_people(request: Request, q: Annotated[str, Query(max_length=80)] = "",
                        exclude: int | None = None):
    if (resp := owner_gate(request)) is not None:
        return resp
    if len(q.strip()) < MIN_QUERY:
        return JSONResponse({"people": []})
    found = [p for p in await search_entities(q.strip(), limit=SEARCH_SHOWN + 1)
             if p["id"] != exclude][:SEARCH_SHOWN]
    summaries = await entity_summaries([p["id"] for p in found])
    return JSONResponse({"people": [summaries[p["id"]] for p in found if p["id"] in summaries]})


@router.post("/api/graph/merge/preview", response_class=JSONResponse)
async def merge_preview(request: Request, body: MergePair):
    if (denied := _gate(request)) is not None:
        return denied
    people = await entity_summaries([body.a, body.b])
    if body.a == body.b or len(people) != 2:
        return _error(MergeError("нужны две разные существующие карточки"))
    recommended = recommend_keep(people[body.a], people[body.b])
    keep = body.keep_id if body.keep_id in (body.a, body.b) else recommended
    drop = body.b if keep == body.a else body.a
    plan = await preview_merge(keep, [drop], REASON)
    return JSONResponse({"keep": people[keep], "drop": people[drop], "recommended_keep": recommended,
                         "counts": plan["counts"], "blockers": plan["blockers"],
                         "would_be_refused": plan["would_be_refused"]})


@router.post("/api/graph/merge/apply", response_class=JSONResponse)
async def merge_apply(request: Request, body: MergeApply):
    if (denied := _gate(request)) is not None:
        return denied
    try:
        result = await apply_merge(body.keep_id, [body.drop_id], REASON, CLIENT)
    except (MergeBlocked, MergeError) as e:
        return _error(e)
    return JSONResponse({"ok": True, "audit_ids": [result["audit_id"]], "counts": result["counts"]})


@router.post("/api/graph/move/preview", response_class=JSONResponse)
async def move_preview(request: Request, body: MovePair):
    if (denied := _gate(request)) is not None:
        return denied
    people = await entity_summaries([body.from_id, body.to_id])
    if body.from_id == body.to_id or len(people) != 2:
        return _error(GraphEditError("нужны две разные существующие карточки"))
    rows = await name_evidence(body.from_id)
    outcomes = {o["rel_id"]: o["outcome"] for o in
                (await preview_move(body.from_id, body.to_id, [r["rel_id"] for r in rows]) if rows else [])}
    return JSONResponse({"from": people[body.from_id], "to": people[body.to_id],
                         "relationships": [{**r, "outcome": outcomes.get(r["rel_id"])} for r in rows]})


@router.post("/api/graph/move/apply", response_class=JSONResponse)
async def move_apply(request: Request, body: MoveApply):
    if (denied := _gate(request)) is not None:
        return denied
    try:
        audit_ids = await move_relationships(body.from_id, body.to_id, body.rel_ids, CLIENT)
    except GraphEditError as e:
        return _error(e)
    return JSONResponse({"ok": True, "audit_ids": audit_ids})
