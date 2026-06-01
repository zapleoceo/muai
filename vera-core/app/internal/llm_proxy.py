"""Internal LLM proxy for other vera-* services.

vera-telegram / vera-gmail don't carry their own LLM pool. When they
need to summarise / classify, they POST here. Auth via X-Internal-Secret
(same secret already used by /event).
"""
import logging

from fastapi import APIRouter, Body, Header, HTTPException

from vera_shared.internal_auth import require_internal as _require_internal
from vera_shared.llm.router import chat as llm_chat

log = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/llm")


@router.post("/chat")
async def llm_chat_proxy(
    payload: dict = Body(...),
    x_internal_secret: str | None = Header(default=None),
) -> dict:
    _require_internal(x_internal_secret)
    messages = payload.get("messages") or []
    system = payload.get("system")
    capability = payload.get("capability", "chat:fast")
    if not messages:
        raise HTTPException(400, "messages required")
    try:
        text = await llm_chat(messages=messages, system=system,
                               capability=capability)
        return {"text": text}
    except Exception as exc:
        log.warning("llm_chat_proxy failed: %s", exc)
        raise HTTPException(502, f"llm error: {exc}") from exc
