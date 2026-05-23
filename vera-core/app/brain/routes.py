"""Identity + editor API."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from app.brain import editor as ED
from app.brain import identity as ID
from app.dashboard.auth import require_owner

router = APIRouter(prefix="/api/brain")


@router.get("/identity")
async def list_identity(_=Depends(require_owner)) -> dict:
    return await ID.list_active()


@router.post("/editor")
async def parse_text(payload: dict = Body(...),
                      _=Depends(require_owner)) -> dict:
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    return await ED.parse_and_apply(text)


@router.post("/identity/{label}/{node_id}/deactivate")
async def deactivate(label: str, node_id: str,
                      _=Depends(require_owner)) -> dict:
    if label not in ("Goal", "Value", "NoGo", "Style"):
        raise HTTPException(400, "label must be Goal|Value|NoGo|Style")
    ok = await ID.deactivate(label, node_id)
    if not ok:
        raise HTTPException(404, "node not found")
    return {"ok": True}
