"""MCP-инструмент делегированного подтверждения авторства (только принципал из ROOM_ATTEST_ACTORS).

HTTP-маршрута /office/v1/.../attest нет намеренно: он требовал бы сервисного токена office,
а у dot его нет, и заводить новый секрет ради этого нельзя.
"""
from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field
from vera_shared.room import tasks
from vera_shared.voice_help import attest as svc

from vera_mcp.auth import client_of
from vera_mcp.room_redact import task_view
from vera_mcp.room_tools import Ident, Room, _room


async def room_task_attest(
    task_id: Ident, qid: Annotated[int, Field(ge=1)],
    expected_revision: Annotated[int, Field(ge=0)],
    evidence_message_id: Annotated[str, Field(min_length=1, max_length=128)],
    evidence_at: Annotated[str, Field(min_length=10, max_length=40)],
    quoted_text: Annotated[str, Field(min_length=1, max_length=svc.MAX_QUOTE)],
    classification: str, idempotency_key: Annotated[str, Field(min_length=1, max_length=128)],
    ctx: Context, room: Room = "main",
) -> dict[str, Any]:
    """Заверить, что владелец сам подтвердил авторство ОДНОЙ голосовой просьбы в чате dot (classification=owner_direct_in_dot_chat). Ответ пишется как dot-attested, не как ответ владельца; допустима только простая просьба ответить владельцу текстом. Только для dot, по флагу ROOM_ATTEST_ENABLED. Attest the owner's authorship confirmation."""
    try:
        return await svc.attest(
            principal=client_of(ctx), room=_room(room), task_id=task_id, qid=qid,
            expected_revision=expected_revision, evidence_message_id=evidence_message_id,
            evidence_at=evidence_at, quoted_text=quoted_text, classification=classification,
            idempotency_key=idempotency_key)
    except svc.AttestError as e:
        raise ValueError(f"{e.code}: {e}") from e


async def room_task_attested_complete(
    task_id: Ident, fencing_token: Annotated[int, Field(ge=1)],
    reply_text: Annotated[str, Field(min_length=1, max_length=100)],
    receipt_id: Ident, ctx: Context, room: Room = "main",
) -> dict[str, Any]:
    """Close a dot-attested task using an existing send receipt. Never sends a reply."""
    result = await tasks.complete_attested(
        room=_room(room), task_id=task_id, agent=client_of(ctx),
        fencing_token=fencing_token, reply_text=reply_text, receipt_id=receipt_id)
    return {"ok": True, **{**result, "task": task_view(result["task"])}}


ATTEST_TOOLS = (room_task_attest, room_task_attested_complete)
