from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo


def _key(user_id: int) -> str:
    return f"user_tz:{int(user_id)}"


async def get_user_timezone(user_id: int | None) -> str:
    if not user_id:
        return "UTC"
    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        v = await repo.get_setting(_key(user_id), default="UTC")
        return str(v or "UTC")


async def set_user_timezone(user_id: int, timezone_name: str) -> None:
    async with AsyncSessionLocal() as session:
        repo = MessageRepo(session)
        await repo.set_setting(_key(user_id), str(timezone_name))
        await session.commit()
