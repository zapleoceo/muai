import asyncio
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event
from sqlalchemy import select, desc


async def main() -> None:
    async with get_session() as s:
        r = await s.execute(select(Event).order_by(desc(Event.id)).limit(50))
        rows = r.scalars().all()
    for e in rows:
        tr = e.triage_result or {}
        uc = tr.get("user_choice") or {}
        execs = tr.get("executions") or []
        body = (e.content_text or "")[:140].replace("\n", " | ")
        line1 = "#%d %s cat=%s status=%s" % (e.id, e.source, e.category, e.triage_status)
        line2 = "   choice: label=%r tool=%r auto=%s" % (
            uc.get("label"), uc.get("tool"), uc.get("auto"))
        print(line1)
        print(line2)
        if execs:
            for x in execs[:2]:
                print("   exec:   tool=%r ok=%s" % (x.get("tool"), x.get("ok")))
        print("   body:   %s" % body)


asyncio.run(main())
