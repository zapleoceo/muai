"""REST API for self-extension dashboard."""
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select, desc

from vera_shared.db.engine import get_session
from vera_shared.db.models import MCPProposal, MCPServer

from app.dashboard.auth import require_owner
from app.self_extend import discovery, proposer, rate_limit, token_watcher

router = APIRouter(prefix="/api/self_extend")


@router.get("/status")
async def status(_=Depends(require_owner)) -> dict:
    async with get_session() as s:
        result = await s.execute(
            select(MCPProposal).order_by(desc(MCPProposal.id)).limit(20)
        )
        recent = result.scalars().all()
        mcps = (await s.execute(select(MCPServer).where(MCPServer.enabled == True))).scalars().all()
    return {
        "rate": await rate_limit.peek(),
        "idle_30d": await token_watcher.find_idle_mcps(30),
        "expired_auth": [
            {"name": m.name, "since": m.last_tool_call_at.isoformat() if m.last_tool_call_at else None}
            for m in mcps if m.auth_state == "token_expired"
        ],
        "proposals": [
            {
                "id": r.id, "capability": r.capability,
                "package_name": r.package_name, "status": r.status,
                "env_required": r.env_required or [],
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "error": r.error,
            }
            for r in recent
        ],
    }


@router.post("/discover")
async def manual_discover(payload: dict = Body(...),
                          _=Depends(require_owner)) -> dict:
    """Owner triggers discovery + proposal manually with free-text capability."""
    cap = (payload.get("capability") or "").strip()
    if not cap:
        raise HTTPException(400, "capability required")
    candidates = await discovery.discover(cap, top_n=3)
    if not candidates:
        return {"ok": False, "reason": "no candidates found", "candidates": []}
    chosen = candidates[0]
    pid = await proposer.create_proposal(cap, chosen)
    return {"ok": True, "proposal_id": pid, "candidates": candidates}


@router.post("/uninstall/{name}")
async def uninstall(name: str, _=Depends(require_owner)) -> dict:
    async with get_session() as s:
        row = (await s.execute(select(MCPServer).where(MCPServer.name == name))).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "mcp server not found")
        await s.delete(row)
        await s.commit()
    from app.mcp import manager
    await manager._stop(name)
    return {"ok": True}
