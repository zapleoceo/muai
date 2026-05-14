from collections.abc import Awaitable, Callable

from app.services.answer_pipeline import run_answer_pipeline
from app.services.answering_types import ReplyResult
from app.services.timezone import get_user_timezone


async def run_ai_reply(
    *,
    chat_id: int,
    user_id: int | None,
    question: str,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> ReplyResult:
    tz = await get_user_timezone(user_id)
    return await run_answer_pipeline(chat_id=chat_id, user_id=user_id, query=question, timezone_name=tz, on_progress=on_progress)
