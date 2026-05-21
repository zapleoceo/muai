"""Dashboard CRUD for MCP servers + manual restart."""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import MCPServer

from app.dashboard.auth import require_owner
from app.mcp import manager
from app.mcp.presets import PRESETS, find as find_preset

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mcp")


@router.get("/servers")
async def list_servers(_=Depends(require_owner)) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(select(MCPServer).order_by(MCPServer.id))
        rows = result.scalars().all()
    return [
        {
            "id": r.id, "name": r.name, "transport": r.transport,
            "command": r.command or [], "url": r.url, "env_keys": list((r.env or {}).keys()),
            "enabled": r.enabled, "status": r.status,
            "error_message": r.error_message, "tools_count": r.tools_count,
            "last_started_at": r.last_started_at.isoformat() if r.last_started_at else None,
        }
        for r in rows
    ]


@router.post("/servers")
async def add_server(payload: dict = Body(...), _=Depends(require_owner)) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    transport = payload.get("transport") or "stdio"
    if transport != "stdio":
        raise HTTPException(400, f"only stdio transport supported (got {transport})")
    command = payload.get("command") or []
    if not isinstance(command, list) or not command:
        raise HTTPException(400, "command must be a non-empty list")
    env = payload.get("env") or {}

    async with get_session() as session:
        row = MCPServer(
            name=name, transport=transport, command=command, env=env, enabled=True,
        )
        session.add(row)
        await session.commit()
    await manager.refresh_from_db()
    return {"ok": True, "name": name}


@router.get("/presets")
async def list_presets(_=Depends(require_owner)) -> list[dict]:
    return PRESETS


@router.post("/presets/{preset_id}")
async def add_from_preset(
    preset_id: str, payload: dict = Body(default={}),
    _=Depends(require_owner),
) -> dict:
    preset = find_preset(preset_id)
    if preset is None:
        raise HTTPException(404, f"preset '{preset_id}' not found")
    env = payload.get("env") or {}
    name = (payload.get("name") or preset_id).strip()
    missing = [k for k in preset["env_required"] if not env.get(k)]
    if missing:
        raise HTTPException(400, f"missing env: {', '.join(missing)}")

    async with get_session() as session:
        row = MCPServer(
            name=name, transport=preset["transport"],
            command=preset["command"], env=env, enabled=True,
        )
        session.add(row)
        await session.commit()
    await manager.refresh_from_db()
    return {"ok": True, "name": name}


@router.delete("/servers/{name}")
async def remove_server(name: str, _=Depends(require_owner)) -> dict:
    async with get_session() as session:
        result = await session.execute(select(MCPServer).where(MCPServer.name == name))
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(404, f"server '{name}' not found")
        await session.delete(row)
        await session.commit()
    await manager.refresh_from_db()
    return {"ok": True}


@router.post("/servers/{name}/restart")
async def restart_server(name: str, _=Depends(require_owner)) -> dict:
    async with get_session() as session:
        result = await session.execute(select(MCPServer).where(MCPServer.name == name))
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(404, f"server '{name}' not found")
    # Force reload: drop + refresh
    await manager._stop(name)
    await manager.refresh_from_db()
    return {"ok": True, "status": manager._servers.get(name, manager.MCPServerHandle(0, name, "stdio")).status}
