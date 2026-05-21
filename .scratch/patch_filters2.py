import asyncio
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models import Source

BOT_USER_ID = 8583101764
BOT_USERNAME = "Dimondra_Ai_Bot"


async def main() -> None:
    async with get_session() as s:
        result = await s.execute(select(Source).where(Source.name == "tg-main"))
        src = result.scalar_one()
        rules = [
            r for r in (src.filters or [])
            if (r.get("match") or {}).get("from_user_id") not in (BOT_USER_ID, 777000)
            and (r.get("match") or {}).get("from_username") != BOT_USERNAME
            and (r.get("match") or {}).get("chat_id") != BOT_USER_ID
        ]
        # Order matters: last matching wins. So put include rules first,
        # then any rules that MUST override (excludes for known noise).
        rules += [
            {"match": {"chat_type": "private", "from_user_id": 777000}, "action": "exclude"},
            {"match": {"from_user_id": BOT_USER_ID}, "action": "exclude"},
            {"match": {"from_username": BOT_USERNAME}, "action": "exclude"},
            {"match": {"chat_id": BOT_USER_ID}, "action": "exclude"},
        ]
        src.filters = rules
        await s.commit()
        print("rules now:")
        for i, r in enumerate(rules):
            print(f"  [{i}] {r}")


asyncio.run(main())
