import asyncio
import json
from sqlalchemy import select, desc
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event


async def main() -> None:
    async with get_session() as s:
        # Anything with an execution recorded
        r = await s.execute(
            select(Event)
            .where(Event.triage_status.in_(["executed", "execute_failed",
                                             "decided", "auto_executed",
                                             "auto_failed"]))
            .order_by(desc(Event.id)).limit(20)
        )
        rows = r.scalars().all()
    for e in rows:
        tr = e.triage_result or {}
        uc = tr.get("user_choice") or {}
        execs = tr.get("executions") or []
        meta = e.metadata_ or {}
        ent = e.entity_hints or []
        sender = next((h.get("identifier") for h in ent if h.get("type") == "person"), None)
        print(f"#{e.id} {e.source} cat={e.category} status={e.triage_status}")
        print(f"  meta.thread_id  = {meta.get('thread_id')}")
        print(f"  meta.message_id = {meta.get('message_id')}")
        print(f"  sender = {sender}")
        print(f"  choice: label={uc.get('label')!r} auto={uc.get('auto')}")
        for x in execs:
            args = x.get("args") or {}
            print(f"  EXEC tool={x.get('tool')} ok={x.get('ok')} "
                  f"args.thread_id={args.get('thread_id')!r} "
                  f"args.peer={args.get('peer')!r} args.to={args.get('to')!r} "
                  f"args.email={args.get('email')!r} "
                  f"args.chat_id={args.get('chat_id')!r}")
            res = x.get("result") or {}
            if isinstance(res, dict):
                preview = json.dumps(res, ensure_ascii=False, default=str)[:200]
            else:
                preview = str(res)[:200]
            print(f"  RESULT preview: {preview}")
        print(f"  body: {(e.content_text or '')[:120]}")
        print()


asyncio.run(main())
