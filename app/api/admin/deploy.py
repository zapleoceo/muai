import asyncio
import re

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
async def get_logs(_uid: int = Depends(require_owner), lines: int = 200, split: bool = False) -> dict:
    raw = await deploy_svc.get_logs(min(2000, max(50, int(lines))) * (6 if split else 1))
    if not split:
        return {"logs": raw}

    embed_pat = re.compile(r"(app\.services\.(embedder|media_embedder)|app\.llm\.embedding|\bEmbedder\b|\bMediaEmbedder\b|\bEmbedding\b)", re.IGNORECASE)
    bot_pat = re.compile(r"(app\.bot\.|aiogram|telethon|app\.logic\.|app\.services\.(answer|router_llm|message_ingest))", re.IGNORECASE)

    embed_lines: list[str] = []
    bot_lines: list[str] = []
    other_lines: list[str] = []

    for line in (raw or "").splitlines():
        if embed_pat.search(line):
            embed_lines.append(line)
        elif bot_pat.search(line):
            bot_lines.append(line)
        else:
            other_lines.append(line)

    lim = min(500, max(20, int(lines)))
    return {
        "embedder": "\n".join(embed_lines[-lim:]),
        "bot": "\n".join(bot_lines[-lim:]),
        "other": "\n".join(other_lines[-lim:]),
    }


@router.post("/admin/migrate")
async def run_migration(background: BackgroundTasks, _uid: int = Depends(require_owner)) -> dict:
    background.add_task(deploy_svc.run_migration)
    return {"status": "migration started"}


@router.post("/admin/deploy")
async def trigger_deploy(_: None = Depends(_require_deploy_auth)) -> dict:
    asyncio.create_task(deploy_svc.run_deploy())
    return {"status": "deploy triggered"}
