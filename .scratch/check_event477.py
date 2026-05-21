import asyncio, json
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event


async def main() -> None:
    async with get_session() as s:
        e = await s.get(Event, 477)
        if e is None:
            print("event 477 missing"); return
        print(f"id={e.id} src={e.source} status={e.triage_status} src_event_id={e.source_event_id}")
        print(f"  account={e.account} category={e.category}")
        print(f"  content head: {(e.content_text or '')[:160]}")
        print(f"  metadata: {json.dumps(e.metadata_, default=str, ensure_ascii=False)[:300]}")
        tr = e.triage_result or {}
        print(f"  triage_result keys: {list(tr.keys())}")
        print(f"  card_message_id: {tr.get('card_message_id')}")


asyncio.run(main())
