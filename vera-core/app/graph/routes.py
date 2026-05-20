import logging
import traceback

from fastapi import APIRouter, Depends

from app.dashboard.auth import require_owner
from app.graph.client import ensure_indices, get_graphiti

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/graph")


@router.get("/health")
async def health(_=Depends(require_owner)) -> dict:
    try:
        client = await get_graphiti()
        await ensure_indices()
        # cheap connection probe via the underlying neo4j driver
        async with client.driver.session() as s:
            res = await s.run("RETURN 1 AS ok")
            row = await res.single()
        return {"ok": True, "neo4j_probe": row["ok"] if row else None}
    except Exception as exc:
        log.warning("graph health failed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc().splitlines()[-5:]}
