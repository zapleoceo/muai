import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException

from app.api.auth import require_owner
from app.config import get_settings
from app.services import deploy as deploy_svc

router = APIRouter()


def _require_deploy_auth(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {get_settings().deploy_secret}"
    if not get_settings().deploy_secret or authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/admin/logs")
async def get_logs(_uid: int = Depends(require_owner), lines: int = 200) -> dict:
    return {"logs": await deploy_svc.get_logs(lines)}


@router.post("/admin/migrate")
async def run_migration(background: BackgroundTasks, _uid: int = Depends(require_owner)) -> dict:
    background.add_task(deploy_svc.run_migration)
    return {"status": "migration started"}


@router.post("/admin/deploy")
async def trigger_deploy(_: None = Depends(_require_deploy_auth)) -> dict:
    asyncio.create_task(deploy_svc.run_deploy())
    return {"status": "deploy triggered"}
