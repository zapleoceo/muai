from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.llm.base import LLMMessage


async def get_dialog_context(chat_id: int, limit: int = 20) -> list[LLMMessage]:
    async with AsyncSessionLocal() as session:
        rows = await MessageRepo(session).get_recent_messages_with_users(chat_id=chat_id, limit=limit)
    return [
        LLMMessage(
            role="assistant" if m.direction == "out" else "user",
            content=m.text or m.caption or f"[{m.media_type or 'media'}]",
        )
        for (m, _u) in rows
    ]
