import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import ExecutorBot, ExecutorChat, ExecutorInbox

logger = logging.getLogger(__name__)

_tasks: dict[int, asyncio.Task] = {}
_bots: dict[int, Bot] = {}
_bot_ids: dict[int, int] = {}      # executor_id -> telegram user_id
_bot_usernames: dict[int, str] = {}  # executor_id -> username (lowercase, no @)
_known_chats: dict[int, set] = {}   # executor_id -> set[chat_id] already in DB
_chat_history: dict[str, deque] = {}  # "{eid}:{chat_id}" -> deque

_HISTORY_SIZE = 15
_CONTEXT_SEND = 10
_HEARTBEAT_INTERVAL = 30


async def _upsert_chat(executor_id: int, chat_id: int, title: str, chat_type: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ExecutorChat).where(
                ExecutorChat.executor_id == executor_id,
                ExecutorChat.chat_id == chat_id,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.chat_title = title
            row.updated_at = datetime.now(timezone.utc)
        else:
            session.add(ExecutorChat(
                executor_id=executor_id,
                chat_id=chat_id,
                chat_title=title,
                chat_type=chat_type,
                can_send=True,
            ))
        await session.commit()


async def _on_message(msg: Message, executor_id: int, bot_username: str) -> None:
    if not msg.chat or msg.chat.type == "private":
        return

    chat_id = msg.chat.id
    chat_title = msg.chat.title or str(chat_id)

    seen = _known_chats.setdefault(executor_id, set())
    if chat_id not in seen:
        seen.add(chat_id)
        asyncio.create_task(_upsert_chat(executor_id, chat_id, chat_title, msg.chat.type))

    text = msg.text or msg.caption or ""
    if text and msg.from_user:
        key = f"{executor_id}:{chat_id}"
        buf = _chat_history.setdefault(key, deque(maxlen=_HISTORY_SIZE))
        buf.append({
            "msg_id": msg.message_id,
            "from": msg.from_user.full_name,
            "text": text,
            "date": msg.date.isoformat() if msg.date else None,
        })

    is_mention = False
    for ent in (msg.entities or []):
        if ent.type == "mention" and msg.text:
            mention_text = msg.text[ent.offset:ent.offset + ent.length]
            if mention_text.lstrip("@").lower() == bot_username:
                is_mention = True
                break

    bot_tg_id = _bot_ids.get(executor_id)
    is_reply_to_bot = bool(
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and bot_tg_id
        and msg.reply_to_message.from_user.id == bot_tg_id
    )

    if not (is_mention or is_reply_to_bot):
        return

    quoted_text: str | None = None
    quoted_from: str | None = None
    if msg.reply_to_message:
        quoted_text = msg.reply_to_message.text or msg.reply_to_message.caption or None
        if msg.reply_to_message.from_user:
            quoted_from = msg.reply_to_message.from_user.full_name

    key = f"{executor_id}:{chat_id}"
    history = _chat_history.get(key)
    context_messages: list[dict] = []
    if history:
        prev = [m for m in history if m["msg_id"] != msg.message_id]
        context_messages = [
            {"from": m["from"], "text": m["text"], "date": m["date"]}
            for m in prev[-_CONTEXT_SEND:]
        ]

    async with AsyncSessionLocal() as session:
        item = ExecutorInbox(
            executor_id=executor_id,
            chat_id=chat_id,
            chat_title=chat_title,
            tg_message_id=msg.message_id,
            from_user_id=msg.from_user.id if msg.from_user else None,
            from_user_name=msg.from_user.full_name if msg.from_user else None,
            text=text,
            is_mention=is_mention,
            reply_to_msg_id=msg.reply_to_message.message_id if msg.reply_to_message else None,
            quoted_text=quoted_text,
            quoted_from=quoted_from,
            context_messages=context_messages or None,
            priority="HIGH" if is_mention else "LOW",
        )
        session.add(item)
        await session.commit()
        item_id = item.id

    logger.info("Inbox item %d (executor=%d chat=%s mention=%s)", item_id, executor_id, chat_id, is_mention)

    if is_mention:
        from app.main import bot as manager_bot
        from app.services.inbox_processor import process_new_item
        asyncio.create_task(process_new_item(item_id, manager_bot))


async def _heartbeat_loop() -> None:
    from app.services.executor_registry import touch
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        now = datetime.now(timezone.utc)
        for eid in list(_tasks.keys()):
            t = _tasks.get(eid)
            if t and not t.done():
                await touch(eid)


async def _run_bot(executor_id: int, bot_token: str) -> None:
    bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    _bots[executor_id] = bot

    try:
        bot_info = await bot.get_me()
        _bot_ids[executor_id] = bot_info.id
        _bot_usernames[executor_id] = (bot_info.username or "").lower()
        logger.info("Bot runner: @%s started (executor_id=%d)", bot_info.username, executor_id)

        dp = Dispatcher()
        dp["executor_id"] = executor_id
        dp["bot_username"] = _bot_usernames[executor_id]

        @dp.message()
        async def on_msg(msg: Message, executor_id: int, bot_username: str) -> None:
            await _on_message(msg, executor_id, bot_username)

        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)

    except asyncio.CancelledError:
        logger.info("Bot runner cancelled (executor_id=%d)", executor_id)
    except Exception:
        logger.exception("Bot runner crashed (executor_id=%d)", executor_id)
    finally:
        _bots.pop(executor_id, None)
        _bot_ids.pop(executor_id, None)
        _bot_usernames.pop(executor_id, None)
        await bot.session.close()


async def start_bot(executor_id: int, bot_token: str) -> None:
    if executor_id in _tasks and not _tasks[executor_id].done():
        return
    task = asyncio.create_task(_run_bot(executor_id, bot_token))
    _tasks[executor_id] = task


async def stop_bot(executor_id: int) -> None:
    task = _tasks.pop(executor_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


def get_bot(executor_id: int) -> Bot | None:
    return _bots.get(executor_id)


def is_running(executor_id: int) -> bool:
    t = _tasks.get(executor_id)
    return bool(t and not t.done())


async def start_all() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ExecutorBot).where(
                ExecutorBot.is_active == True,  # noqa: E712
                ExecutorBot.bot_token.isnot(None),
            )
        )
        bots = result.scalars().all()

    for b in bots:
        await start_bot(b.id, b.bot_token)

    asyncio.create_task(_heartbeat_loop())
    logger.info("BotRunner started %d bot(s)", len(bots))


async def stop_all() -> None:
    for eid in list(_tasks.keys()):
        await stop_bot(eid)
    logger.info("BotRunner stopped all bots")
