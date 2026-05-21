from fastapi import APIRouter, Depends

from app.dashboard.auth import require_owner
from app.persona import digest

router = APIRouter(prefix="/api/persona")


@router.get("")
async def get_persona(_=Depends(require_owner)) -> dict:
    return await digest.current() or {"bullets": [], "covers_events": 0}


@router.post("/regenerate")
async def regenerate(_=Depends(require_owner)) -> dict:
    return await digest.regenerate()
