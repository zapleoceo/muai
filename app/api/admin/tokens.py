from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.auth import require_owner
from app.llm.capabilities import effective_capabilities, normalize_capabilities
from app.services.tokens import get_token_manager

router = APIRouter()


class TokenIn(BaseModel):
    token: str
    label: str = ""
    provider: str = "gemini"
    capabilities: list[str] | None = None


class TokenCapsPatch(BaseModel):
    capabilities: list[str] | None = None


@router.get("/admin/tokens")
async def list_tokens(
    _uid: int = Depends(require_owner),
    provider: str | None = Query(default=None),
) -> list[dict]:
    manager = get_token_manager()
    rows = await manager.list_tokens(provider=provider)
    statuses = manager.slot_status()
    result = []
    for r in rows:
        live = statuses.get(r.id)
        caps_raw = r.capabilities if isinstance(r.capabilities, list) else None
        caps = sorted(effective_capabilities(r.provider, caps_raw))
        result.append({
            "id": r.id,
            "provider": r.provider,
            "label": r.label or "",
            "capabilities": live["capabilities"] if live and "capabilities" in live else caps,
            "masked": f"{r.token[:8]}...{r.token[-4:]}" if len(r.token) > 12 else "***",
            "is_active": r.is_active,
            "status": live["status"] if live else ("inactive" if not r.is_active else "active"),
            "requests_today": live["requests_today"] if live else 0,
            "daily_limit": live["daily_limit"] if live else (1500 if r.provider == "gemini" else 0),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return result


@router.post("/admin/tokens")
async def add_token(body: TokenIn, _uid: int = Depends(require_owner)) -> dict:
    if not body.token.strip():
        raise HTTPException(status_code=400, detail="Token cannot be empty")
    caps = normalize_capabilities(body.provider, body.capabilities)
    row = await get_token_manager().add(body.token.strip(), body.label.strip(), body.provider, capabilities=caps)
    return {"id": row.id, "label": row.label, "provider": row.provider}


@router.delete("/admin/tokens/{token_id}")
async def delete_token(token_id: int, _uid: int = Depends(require_owner)) -> dict:
    ok = await get_token_manager().remove(token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"deleted": token_id}


@router.patch("/admin/tokens/{token_id}/toggle")
async def toggle_token(token_id: int, _uid: int = Depends(require_owner)) -> dict:
    row = await get_token_manager().toggle(token_id)
    if not row:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"id": row.id, "is_active": row.is_active}


@router.patch("/admin/tokens/{token_id}/capabilities")
async def update_token_capabilities(
    token_id: int,
    body: TokenCapsPatch,
    _uid: int = Depends(require_owner),
) -> dict:
    row = await get_token_manager().update_capabilities(token_id, body.capabilities)
    if not row:
        raise HTTPException(status_code=404, detail="Token not found")
    caps_raw = row.capabilities if isinstance(row.capabilities, list) else None
    caps = normalize_capabilities(row.provider, caps_raw)
    return {"id": row.id, "capabilities": caps}
