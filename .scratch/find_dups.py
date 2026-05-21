"""Diagnostic: count how many separate Telegram messages were sent per
event_id (= duplicate cards), and reset any pending duplicates."""
import asyncio
from sqlalchemy import select, func
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event


async def main() -> None:
    async with get_session() as s:
        # Find events where triage_result has card_message_id but the
        # status moved to decided/executed yet other invocations would
        # have overwritten the card_message_id. We can't get history of
        # those — just report current statuses.
        r = await s.execute(
            select(Event.triage_status, func.count())
            .group_by(Event.triage_status)
        )
        for status, n in r.all():
            print(f"{status:20s} {n}")
        print()

        # Specifically the noisy ones from logs (event 477+):
        for eid in [477, 478, 479, 480, 481]:
            ev = await s.get(Event, eid)
            if ev:
                tr = ev.triage_result or {}
                print(f"#{eid}: status={ev.triage_status} card={tr.get('card_message_id')} "
                      f"user_choice={tr.get('user_choice', {}).get('label') if tr.get('user_choice') else None}")


asyncio.run(main())
