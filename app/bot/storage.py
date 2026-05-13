from app.db.database import AsyncSessionLocal
from app.db.repository import MessageRepo
from app.llm.base import LLMMessage


async def get_dialog_context(chat_id: int, limit: int = 20) -> list[LLMMessage]:
    async with AsyncSessionLocal() as session:
        rows = await MessageRepo(session).get_messages(chat_id=chat_id, limit=limit)
    return [
        LLMMessage(
            role="assistant" if r.direction == "out" else "user",
            content=r.text or r.caption or f"[{r.media_type or 'media'}]",
        )
        for r in rows
    ]
