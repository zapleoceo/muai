"""Разрушительные MCP-правки ждут подтверждения владельца в дашборде.

Агент читает недоверенный текст писем и чатов, поэтому вызов `entity_merge`,
`hide_event` или `relationship_retire` мог прийти из инъекции. Инструмент
только записывает предложение, а применяет его владелец на `/approvals`
(сессия владельца + same-origin): этого шага инъекция сама сделать не может.

Предложение — строка того же журнала `mcp_audit` с `target_kind='proposal'`,
поэтому миграция не нужна, а `undo` такую строку не трогает (нет обработчика
для этого вида). Применение пишет обычную запись журнала от имени того же
клиента, и её откатывает прежний `undo`/`entity_unmerge`.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, update

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import RelationshipRow
from vera_shared.db.models_mcp import McpAuditRow
from vera_shared.events import edit as event_edit
from vera_shared.graph import edit as graph_edit
from vera_shared.graph.merge_actions import apply_merge, preview_merge
from vera_shared.graph.merge_guard import MergeBlocked
from vera_shared.journal import audit

log = logging.getLogger(__name__)

PROPOSAL_KIND = "proposal"
PROPOSED, APPLYING, APPROVED, REJECTED = "proposed", "applying", "approved", "rejected"
APPROVALS_PATH = "/approvals"
PREVIEW_CHARS = 200


class ProposalNotPending(LookupError):
    def __init__(self, proposal_id: int) -> None:
        super().__init__(f"proposal {proposal_id} is not pending")


async def _apply_hide(client: str, args: dict[str, Any]) -> dict[str, Any]:
    event_id = int(args["event_id"])
    async with get_session() as s:
        before, after = await event_edit.set_hidden(s, event_id, hidden=True)
        audit_id = await audit.record(s, client=client, tool="hide_event", args=args,
                                      kind="event", target_id=event_id,
                                      before=before, after=after)
    return {"ok": True, "audit_id": audit_id, "hidden": True}


async def _apply_retire(client: str, args: dict[str, Any]) -> dict[str, Any]:
    rel_id = int(args["relationship_id"])
    async with get_session() as s:
        before, after = await graph_edit.retire_relationship(s, rel_id)
        audit_id = await audit.record(s, client=client, tool="relationship_retire",
                                      args=args, kind="relationship", target_id=rel_id,
                                      before=before, after=after)
    return {"ok": True, "audit_id": audit_id, "relationship_id": rel_id}


async def _apply_merge(client: str, args: dict[str, Any]) -> dict[str, Any]:
    return await apply_merge(int(args["keep_id"]), [int(i) for i in args["drop_ids"]],
                             str(args["reason"]), client, bool(args.get("force")))


_APPLY: dict[str, Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "hide_event": _apply_hide,
    "relationship_retire": _apply_retire,
    "entity_merge": _apply_merge,
}


async def _check_target(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Ошибку цели видно сразу, а не при подтверждении; для слияния — предпросмотр."""
    if tool == "entity_merge":
        preview = await preview_merge(int(args["keep_id"]), list(args["drop_ids"]),
                                      str(args["reason"]), bool(args.get("force")))
        if preview["would_be_refused"]:
            raise MergeBlocked("; ".join(preview["blockers"])
                               + " — pass force=true to merge anyway")
        return preview
    async with get_session() as s:
        if tool == "hide_event":
            row = await event_edit.load_row(s, int(args["event_id"]))
            return {"source": row.source, "occurred_at": str(row.occurred_at),
                    "excerpt": (row.content_text or "")[:PREVIEW_CHARS]}
        rel = await s.get(RelationshipRow, int(args["relationship_id"]))
        if rel is None:
            raise graph_edit.GraphEditError(
                f"relationship {args['relationship_id']} not found")
        return graph_edit.relationship_snapshot(rel)


async def propose(client: str, tool: str, args: dict[str, Any],
                  target_id: int) -> dict[str, Any]:
    if tool not in _APPLY:
        raise ValueError(f"tool '{tool}' does not go through owner approval")
    preview = await _check_target(tool, args)
    async with get_session() as s:
        row = McpAuditRow(client=client, tool=tool, args=args, target_kind=PROPOSAL_KIND,
                          target_id=target_id, before=None, after={"preview": preview},
                          status=PROPOSED)
        s.add(row)
        await s.flush()
        proposal_id = row.id
    log.info("mcp proposal %s: client=%s tool=%s target=%s awaits owner",
             proposal_id, client, tool, target_id)
    return {"ok": True, "audit_id": None, "proposal_id": proposal_id,
            "status": "pending_owner_approval", "approve_at": APPROVALS_PATH,
            "note": "Not applied. The owner must approve it in the dashboard."}


async def pending(limit: int = 100) -> list[McpAuditRow]:
    async with get_session() as s:
        return list((await s.execute(
            select(McpAuditRow)
            .where(McpAuditRow.target_kind == PROPOSAL_KIND, McpAuditRow.status == PROPOSED)
            .order_by(McpAuditRow.id).limit(limit)
        )).scalars().all())


async def _claim(proposal_id: int, new_status: str) -> McpAuditRow:
    """Атомарно proposed → new_status: двойной клик не применит правку дважды."""
    async with get_session() as s:
        changed = (await s.execute(
            update(McpAuditRow)
            .where(McpAuditRow.id == proposal_id, McpAuditRow.target_kind == PROPOSAL_KIND,
                   McpAuditRow.status == PROPOSED)
            .values(status=new_status)
        )).rowcount
        if changed != 1:
            raise ProposalNotPending(proposal_id)
        return (await s.execute(
            select(McpAuditRow).where(McpAuditRow.id == proposal_id))).scalar_one()


async def _finish(proposal_id: int, status: str, after: dict[str, Any]) -> None:
    async with get_session() as s:
        row = (await s.execute(
            select(McpAuditRow).where(McpAuditRow.id == proposal_id).with_for_update()
        )).scalar_one()
        row.status = status
        row.after = {**(row.after or {}), **after}


async def approve(proposal_id: int) -> dict[str, Any]:
    row = await _claim(proposal_id, APPLYING)
    try:
        result = await _APPLY[row.tool](row.client, dict(row.args))
    except Exception:
        await _finish(proposal_id, PROPOSED, {})
        raise
    await _finish(proposal_id, APPROVED, {"applied_audit_id": result.get("audit_id")})
    log.info("mcp proposal %s approved by owner: applied audit=%s",
             proposal_id, result.get("audit_id"))
    return {**result, "proposal_id": proposal_id}


async def reject(proposal_id: int) -> dict[str, Any]:
    await _claim(proposal_id, REJECTED)
    log.info("mcp proposal %s rejected by owner", proposal_id)
    return {"ok": True, "proposal_id": proposal_id, "status": REJECTED}
