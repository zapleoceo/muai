from fastapi import APIRouter, Depends

from app.api.auth import require_owner
from app.services import stats as stats_svc

router = APIRouter()


@router.get("/admin/stats")
async def get_stats(_uid: int = Depends(require_owner)) -> dict:
    return await stats_svc.get_dashboard_stats()
