"""Internal LLM proxy — lets VERA and other internal services use myAI key pool."""
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.llm.factory import get_llm_provider

logger = logging.getLogger(__name__)
router = APIRouter()


class _Message(BaseModel):
    role: str
    content: str


class _CompleteRequest(BaseModel):
    messages: list[_Message]


def _auth(authorization: str | None = Header(default=None)) -> None:
    secret = get_settings().api_secret_key
    if not secret or authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/internal/llm/complete")
async def llm_complete(body: _CompleteRequest, _: None = Depends(_auth)) -> dict:
    from app.llm.base import LLMMessage
    provider = get_llm_provider()
    msgs = [LLMMessage(role=m.role, content=m.content) for m in body.messages]
    result = await provider.complete(msgs)
    return {"text": result}
