import asyncio
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models import Source

BOT_USER_ID = 8583101764
BOT_USERNAME = "Dimondra_Ai_Bot"


async def main() -> None:
    async with get_session() as s:
        result = await s.execute(select(Source).where(Source.name == "tg-main"))
        src = result.scalar_one_or_none()
        if src is None:
            print("source tg-main not found")
            return
        rules = list(src.filters or [])
        already = any(
            (r.get("match") or {}).get("from_user_id") == BOT_USER_ID
            for r in rules
        )
        if not already:
            # Insert exclude rules at the FRONT so they catch even before
            # mention_me / reply_to_me priority rules fire.
            rules = [
                {"match": {"from_user_id": BOT_USER_ID}, "action": "exclude"},
                {"match": {"from_username": BOT_USERNAME}, "action": "exclude"},
                {"match": {"chat_id": BOT_USER_ID}, "action": "exclude"},
            ] + rules
            src.filters = rules
            await s.commit()
            print("filters patched:", len(rules), "rules")
        else:
            print("filters already include bot exclusion")
        for i, r in enumerate(src.filters):
            print(f"  [{i}] {r}")


asyncio.run(main())
