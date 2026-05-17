import asyncio
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.auth import require_owner
from app.config import get_settings
from app.db.database import AsyncSessionLocal
from app.db.models import ExecutorInbox
from app.services.executor_registry import list_executors, register_or_update, touch, update_bot_settings

router = APIRouter()
logger = logging.getLogger(__name__)


def _verify_inbox_secret(authorization: str) -> None:
    secret = get_settings().executor_inbox_secret
    if not secret or authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")


class RegisterPayload(BaseModel):
    name: str
    bot_username: str
    api_url: str
    api_secret: str
    chats: list[dict]


class HeartbeatPayload(BaseModel):
    executor_id: int


class InboxPayload(BaseModel):
    executor_id: int
    chat_id: int
    chat_title: str | None = None
    tg_message_id: int
    from_user_id: int | None = None
    from_user_name: str | None = None
    text: str | None = None
    is_mention: bool
    reply_to_msg_id: int | None = None
    quoted_text: str | None = None
    quoted_from: str | None = None
    context_messages: list[dict] | None = None


class BotSettingsPayload(BaseModel):
    forward_mode: str | None = None
    is_enabled: bool | None = None


@router.post("/executor/register")
async def executor_register(
    payload: RegisterPayload,
    authorization: str = Header(default=""),
) -> dict:
    _verify_inbox_secret(authorization)
    executor_id = await register_or_update(
        name=payload.name,
        bot_username=payload.bot_username,
        api_url=payload.api_url,
        api_secret=payload.api_secret,
        chats=payload.chats,
    )
    return {"ok": True, "executor_id": executor_id}


@router.post("/executor/heartbeat")
async def executor_heartbeat(
    payload: HeartbeatPayload,
    authorization: str = Header(default=""),
) -> dict:
    _verify_inbox_secret(authorization)
    await touch(payload.executor_id)
    return {"ok": True}


@router.post("/executor/inbox")
async def executor_inbox(
    payload: InboxPayload,
    authorization: str = Header(default=""),
) -> dict:
    _verify_inbox_secret(authorization)

    async with AsyncSessionLocal() as session:
        item = ExecutorInbox(
            executor_id=payload.executor_id,
            chat_id=payload.chat_id,
            chat_title=payload.chat_title,
            tg_message_id=payload.tg_message_id,
            from_user_id=payload.from_user_id,
            from_user_name=payload.from_user_name,
            text=payload.text,
            is_mention=payload.is_mention,
            reply_to_msg_id=payload.reply_to_msg_id,
            quoted_text=payload.quoted_text,
            quoted_from=payload.quoted_from,
            context_messages=payload.context_messages,
            priority="HIGH" if payload.is_mention else "LOW",
        )
        session.add(item)
        await session.commit()
        item_id: int = item.id

    if payload.is_mention:
        from app.main import bot  # import here to avoid circular at module load
        from app.services.inbox_processor import process_new_item
        asyncio.create_task(process_new_item(item_id, bot))

    return {"ok": True, "item_id": item_id}


@router.get("/admin/executor/bots")
async def list_bots(user=Depends(require_owner)) -> list:
    return await list_executors()


@router.patch("/admin/executor/bots/{bot_id}")
async def patch_bot(
    bot_id: int,
    payload: BotSettingsPayload,
    user=Depends(require_owner),
) -> dict:
    await update_bot_settings(bot_id, forward_mode=payload.forward_mode, is_enabled=payload.is_enabled)
    return {"ok": True}


@router.get("/admin/executor/inbox")
async def list_inbox(user=Depends(require_owner), limit: int = 50) -> list:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ExecutorInbox).order_by(ExecutorInbox.created_at.desc()).limit(limit)
        )
        items = result.scalars().all()
    return [
        {
            "id": i.id,
            "executor_id": i.executor_id,
            "chat_id": i.chat_id,
            "chat_title": i.chat_title,
            "from_user_name": i.from_user_name,
            "text": i.text,
            "is_mention": i.is_mention,
            "quoted_text": i.quoted_text,
            "quoted_from": i.quoted_from,
            "priority": i.priority,
            "status": i.status,
            "draft_reply": i.draft_reply,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        }
        for i in items
    ]
