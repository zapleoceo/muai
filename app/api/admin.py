import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel

from app.api.auth import require_owner
from app.config import get_settings
from app.services import deploy as deploy_svc
from app.services import stats as stats_svc
from app.services.tokens import get_token_manager

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_deploy_auth(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {get_settings().deploy_secret}"
    if not get_settings().deploy_secret or authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── stats / logs / deploy ─────────────────────────────────────────────────────

@router.get("/admin/stats")
async def get_stats(_uid: int = Depends(require_owner)) -> dict:
    return await stats_svc.get_dashboard_stats()


@router.get("/admin/logs")
async def get_logs(_uid: int = Depends(require_owner), lines: int = 200) -> dict:
    return {"logs": await deploy_svc.get_logs(lines)}


@router.post("/admin/migrate")
async def run_migration(background: BackgroundTasks, _uid: int = Depends(require_owner)) -> dict:
    background.add_task(deploy_svc.run_migration)
    return {"status": "migration started"}


@router.post("/admin/deploy")
async def trigger_deploy(_: None = Depends(_require_deploy_auth)) -> dict:
    import asyncio
    asyncio.create_task(deploy_svc.run_deploy())
    return {"status": "deploy triggered"}


# ── token management ──────────────────────────────────────────────────────────

class TokenIn(BaseModel):
    token: str
    label: str = ""
    provider: str = "gemini"


@router.get("/admin/tokens")
async def list_tokens(_uid: int = Depends(require_owner)) -> list[dict]:
    manager = get_token_manager()
    rows = await manager.list_tokens()
    statuses = manager.slot_status()
    return [
        {
            "id": r.id,
            "provider": r.provider,
            "label": r.label or "",
            "masked": f"{r.token[:8]}...{r.token[-4:]}" if len(r.token) > 12 else "***",
            "is_active": r.is_active,
            "status": statuses.get(r.id, "inactive" if not r.is_active else "active"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/admin/tokens")
async def add_token(body: TokenIn, _uid: int = Depends(require_owner)) -> dict:
    if not body.token.strip():
        raise HTTPException(status_code=400, detail="Token cannot be empty")
    row = await get_token_manager().add(body.token.strip(), body.label.strip(), body.provider)
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
