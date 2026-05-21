import asyncio
import json
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event, Source
from sqlalchemy import select


async def main() -> None:
    async with get_session() as s:
        ev = await s.get(Event, 412)
        if ev:
            print("Event 412 metadata:")
            print(json.dumps(ev.metadata_, ensure_ascii=False, indent=2, default=str))
            print("entity_hints:")
            print(json.dumps(ev.entity_hints, ensure_ascii=False, indent=2, default=str))
            print("category:", ev.category)
        print("\n\n--- Sources ---")
        r = await s.execute(select(Source))
        for src in r.scalars():
            print(f"id={src.id} name={src.name} type={src.type} enabled={src.enabled}")
            print(f"  filters: {json.dumps(src.filters, ensure_ascii=False, indent=2)}")
            print(f"  intake_count={src.intake_count} last_polled={src.last_polled_at}")


asyncio.run(main())
