import asyncio
import json
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models import Source

BOT_USER_ID = 8583101764
BOT_USERNAME = "Dimondra_Ai_Bot"


async def main() -> None:
    async with get_session() as s:
        result = await s.execute(select(Source).where(Source.name == "tg-main"))
        src = result.scalar_one()
        # Engine semantics: last matching rule wins, default exclude.
        # Goal: private chats included; in groups only when @mention me;
        # never react to bot's own messages.
        src.filters = [
            # Include private 1-on-1 chats with humans
            {"match": {"chat_type": "private"}, "action": "include"},
            # ANY chat: explicit mention is high-signal
            {"match": {"mention_me": True}, "action": "priority"},
            # Private chat: replies to my message are signal (NOT in groups —
            # there reply_to_me triggers on every reply to ANY of my old msgs)
            {"match": {"chat_type": "private", "reply_to_me": True}, "action": "priority"},
            # Negatives (must come last to override includes)
            {"match": {"chat_type": "private", "from_user_id": 777000}, "action": "exclude"},
            {"match": {"from_user_id": BOT_USER_ID}, "action": "exclude"},
            {"match": {"from_username": BOT_USERNAME}, "action": "exclude"},
            {"match": {"chat_id": BOT_USER_ID}, "action": "exclude"},
        ]
        await s.commit()
        print(json.dumps(src.filters, ensure_ascii=False, indent=2))


asyncio.run(main())
