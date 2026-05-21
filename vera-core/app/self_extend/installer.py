"""Installer: insert MCPServer row, ask manager to refresh, run a safe
smoke test, rollback on failure. Rate-limited."""
import logging
from datetime import datetime

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import MCPProposal, MCPServer

from app.mcp import manager
from app.self_extend.rate_limit import check_and_consume

log = logging.getLogger(__name__)


async def install_now(proposal_id: int) -> str:
    allowed, reason = await check_and_consume("install")
    if not allowed:
        await _mark(proposal_id, "failed", reason)
        return f"⛔ Лимит: {reason}. Попробуй позже."

    async with get_session() as s:
        prop = await s.get(MCPProposal, proposal_id)
        if prop is None:
            return "⚠️ Предложение исчезло."

    info = prop.package_info or {}
    command = info.get("command") or []
    env = dict(prop.env_collected or {})
    name = prop.package_name or info.get("name") or f"self-extend-{proposal_id}"

    async with get_session() as s:
        # Conflict check
        existing = await s.execute(select(MCPServer).where(MCPServer.name == name))
        if existing.scalar_one_or_none() is not None:
            await _mark(proposal_id, "failed", f"name conflict: {name}")
            return f"⚠️ MCP с именем {name} уже есть."
        row = MCPServer(
            name=name, transport="stdio", command=command,
            env=env, enabled=True, installed_by="self_extend",
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        new_id = row.id

    await _mark(proposal_id, "installing", mcp_server_id=new_id)

    try:
        await manager.refresh_from_db()
    except Exception as exc:
        log.exception("manager.refresh_from_db crashed: %s", exc)
        await _rollback(new_id, proposal_id, f"refresh crashed: {exc}")
        return f"⚠️ Установка не удалась: {exc}"

    handle = manager._servers.get(name)
    if handle is None or handle.status != "running":
        err = (handle.error if handle else "manager did not start the server")
        await _rollback(new_id, proposal_id, f"start failed: {err}")
        return f"⚠️ Сервер не стартовал: {err}"
    if not handle.tools:
        await _rollback(new_id, proposal_id, "started but exposed 0 tools")
        return "⚠️ Сервер стартовал, но не дал ни одного tool — откатываю."

    await _mark(proposal_id, "active", mcp_server_id=new_id)
    n = len(handle.tools)
    tool_names = ", ".join(t["name"] for t in handle.tools[:5])
    suffix = "…" if n > 5 else ""
    return (f"✅ Установлено: <code>{name}</code> — {n} инструментов "
            f"({tool_names}{suffix}). Возвращаюсь к работе.")


async def _rollback(mcp_id: int, proposal_id: int, error: str) -> None:
    log.warning("Self-extend rollback for proposal %s: %s", proposal_id, error)
    try:
        async with get_session() as s:
            row = await s.get(MCPServer, mcp_id)
            if row:
                name = row.name
                await s.delete(row)
                await s.commit()
                try:
                    await manager._stop(name)
                except Exception:
                    pass
    except Exception as exc:
        log.exception("rollback DB delete failed: %s", exc)
    await _mark(proposal_id, "failed", error)


async def _mark(proposal_id: int, status: str, error: str | None = None,
                mcp_server_id: int | None = None) -> None:
    async with get_session() as s:
        row = await s.get(MCPProposal, proposal_id)
        if not row:
            return
        row.status = status
        if status == "installing" and not row.decided_at:
            row.decided_at = datetime.utcnow()
        if status in ("active", "rejected", "failed", "uninstalled"):
            row.completed_at = datetime.utcnow()
        if error:
            row.error = error[:500]
        if mcp_server_id is not None:
            row.mcp_server_id = mcp_server_id
        await s.commit()
