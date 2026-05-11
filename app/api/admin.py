import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from sqlalchemy import func, select, text

from app.api.auth import require_owner
from app.config import get_settings
from app.db.database import AsyncSessionLocal
from app.db.models import Chat, Message, TgUser

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


def _check_deploy_auth(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {settings.deploy_secret}"
    if not settings.deploy_secret or authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/admin/stats")
async def stats(_uid: int = Depends(require_owner)) -> dict:
    async with AsyncSessionLocal() as session:
        total_messages = (await session.execute(select(func.count()).select_from(Message))).scalar()
        total_chats    = (await session.execute(select(func.count()).select_from(Chat))).scalar()
        total_users    = (await session.execute(select(func.count()).select_from(TgUser))).scalar()

        since = datetime.now(tz=timezone.utc) - timedelta(days=7)
        daily = (await session.execute(
            text("""
                SELECT date_trunc('day', date_utc) AS day, COUNT(*) AS cnt
                FROM messages WHERE date_utc >= :since
                GROUP BY day ORDER BY day
            """),
            {"since": since},
        )).all()

        top_chats = (await session.execute(
            text("""
                SELECT c.title, c.type, COUNT(m.id) AS cnt
                FROM messages m JOIN chats c ON c.id = m.chat_id
                WHERE m.date_utc >= :since
                GROUP BY c.id, c.title, c.type
                ORDER BY cnt DESC LIMIT 10
            """),
            {"since": since},
        )).all()

        in_count  = (await session.execute(select(func.count()).select_from(Message).where(Message.direction == "in"))).scalar()
        out_count = (await session.execute(select(func.count()).select_from(Message).where(Message.direction == "out"))).scalar()

    return {
        "totals": {"messages": total_messages, "chats": total_chats, "users": total_users,
                   "incoming": in_count, "outgoing": out_count},
        "daily":     [{"day": str(r.day)[:10], "count": r.cnt} for r in daily],
        "top_chats": [{"title": r.title or r.type, "type": r.type, "count": r.cnt} for r in top_chats],
    }


@router.get("/admin/logs")
async def get_logs(_uid: int = Depends(require_owner), lines: int = 200) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", "/var/www/tgbot/docker-compose.yml",
            "logs", "bot", f"--tail={lines}", "--no-log-prefix",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return {"logs": stdout.decode(errors="replace")}
    except Exception as e:
        return {"logs": f"Error fetching logs: {e}"}


@router.post("/admin/migrate")
async def run_migration(background: BackgroundTasks, _uid: int = Depends(require_owner)) -> dict:
    async def _migrate():
        proc = await asyncio.create_subprocess_exec(
            "alembic", "upgrade", "head", cwd="/app",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        logger.info("Migration:\n%s", out.decode(errors="replace"))
    background.add_task(_migrate)
    return {"status": "migration started"}


@router.post("/admin/deploy")
async def deploy(_: None = Depends(_check_deploy_auth)) -> dict:
    asyncio.create_task(_run_deploy())
    return {"status": "deploy triggered"}


async def _run_deploy() -> None:
    cmds = [
        ["git", "-C", "/var/www/tgbot", "pull"],
        ["docker", "compose", "-f", "/var/www/tgbot/docker-compose.yml", "build", "bot"],
        ["docker", "compose", "-f", "/var/www/tgbot/docker-compose.yml", "up", "-d", "bot"],
    ]
    for cmd in cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        logger.info("Deploy [%s]:\n%s", " ".join(cmd), out.decode(errors="replace"))
