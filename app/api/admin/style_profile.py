from fastapi import APIRouter, Depends

from app.api.auth import require_owner
from app.services.style_profile import build_style_profile, get_style_profile

router = APIRouter()


@router.get("/admin/style-profile")
async def get_profile(_uid: int = Depends(require_owner)) -> dict:
    profile = await get_style_profile()
    return {"profile": profile, "has_profile": profile is not None}


@router.post("/admin/style-profile/build")
async def rebuild_profile(_uid: int = Depends(require_owner)) -> dict:
    profile = await build_style_profile()
    return {"profile": profile, "length": len(profile)}
