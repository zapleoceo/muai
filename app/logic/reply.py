from app.services.answer_pipeline import run_answer_pipeline
from app.services.answering_types import ReplyResult


async def run_ai_reply(*, chat_id: int, user_id: int | None, question: str) -> ReplyResult:
    return await run_answer_pipeline(chat_id=chat_id, user_id=user_id, query=question)
