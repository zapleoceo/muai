"""`/approvals` — владелец подтверждает правки агентов, которые сами не применяются.

Инъекция в письме может заставить MCP-клиента вызвать `entity_merge`,
`hide_event`, `relationship_retire` или агента поиска — `memory.remember`, но
применить их она не может: это делает только POST владельца отсюда, за
`owner_post_gate` (сессия владельца + same-origin). Логика —
`vera_shared.journal.proposals` и `vera_shared.memory.agent_memory`.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from vera_shared.journal import proposals
from vera_shared.memory import agent_memory

from dashboard.approvals_view import approvals_body
from dashboard.csrf import owner_post_gate
from dashboard.render import _render, owner_or_redirect

router = APIRouter()
CLIENT = "dashboard"
SHOWN = 100


class Decision(BaseModel):
    kind: Literal["proposal", "memory"]
    id: int = Field(ge=1)
    approve: bool


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp
    pending = await proposals.pending(SHOWN)
    memories = await agent_memory.pending_agent_memories(SHOWN)
    return HTMLResponse(_render("journal", approvals_body(pending, memories)))


@router.post("/api/approvals/decide", response_class=JSONResponse)
async def decide(request: Request, body: Decision):
    if (denied := owner_post_gate(request)) is not None:
        return denied
    try:
        if body.kind == "proposal":
            result = await (proposals.approve(body.id) if body.approve
                            else proposals.reject(body.id))
        elif body.approve:
            result = await agent_memory.confirm_agent_memory(body.id, CLIENT)
        else:
            result = await agent_memory.reject_agent_memory(body.id, CLIENT)
    except (ValueError, LookupError) as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return JSONResponse(result)
