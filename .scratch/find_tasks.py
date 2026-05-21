import asyncio
import json
from sqlalchemy import select, desc
from vera_shared.db.engine import get_session
from vera_shared.db.models import Task


async def main() -> None:
    async with get_session() as s:
        r = await s.execute(select(Task).order_by(desc(Task.id)).limit(10))
        for t in r.scalars():
            print(f"--- task #{t.id} created={t.created_at} ---")
            print(f"prompt: {(t.prompt or '')[:200]}")
            try:
                trace = t.trace or []
                if isinstance(trace, str):
                    trace = json.loads(trace)
                for step in trace[:8]:
                    print(f"  step: tool={step.get('tool')} args={list((step.get('args') or {}).items())[:3]} ok={step.get('ok')}")
            except Exception as ex:
                print("  (trace parse failed:", ex, ")")
            print(f"reply: {(t.reply or '')[:300]}")
            print()


asyncio.run(main())
