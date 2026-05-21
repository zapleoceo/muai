import asyncio
import json
from sqlalchemy import select, desc
from vera_shared.db.engine import get_session
from vera_shared.db.models import Task


async def main() -> None:
    async with get_session() as s:
        r = await s.execute(select(Task).order_by(desc(Task.id)).limit(15))
        rows = r.scalars().all()
    for t in rows:
        d = {c.name: getattr(t, c.name) for c in t.__table__.columns}
        print(f"--- task #{d.get('id')} created={d.get('created_at')} ---")
        for k, v in d.items():
            if k in ("trace", "tokens_used", "id", "created_at"):
                continue
            s = str(v)
            if len(s) > 200:
                s = s[:200] + "…"
            print(f"  {k}: {s}")
        trace = d.get("trace") or []
        if isinstance(trace, str):
            try: trace = json.loads(trace)
            except Exception: trace = []
        if trace:
            print("  trace:")
            for step in trace[:8]:
                args = step.get("args") or {}
                preview = ", ".join(f"{k}={str(v)[:60]}" for k,v in list(args.items())[:4])
                print(f"    {step.get('tool')}({preview}) ok={step.get('ok')}")
        print()


asyncio.run(main())
