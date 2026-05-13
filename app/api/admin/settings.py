from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import require_owner
from app.services import chat_settings as cs

router = APIRouter()


@router.get("/admin/settings/sync")
async def get_sync_settings(_uid: int = Depends(require_owner)) -> dict:
    return await cs.get_global_settings()


class SyncSettingsPatch(BaseModel):
    allowed_types: list[str] | None = None
    blacklist: list[str | int] | None = None
    default_depth_days: int | None = None


@router.patch("/admin/settings/sync")
async def update_sync_settings(
    body: SyncSettingsPatch,
    _uid: int = Depends(require_owner),
) -> dict:
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")
    return await cs.update_global_settings(patch)
