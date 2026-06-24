"""Перезаполнить расшаренные карточки: text + link_url + preview_url из IG.
argv: число = один чат; иначе все чаты с карточками (по эмодзи-префиксу / [..]).
Идемпотентно, update-on-change. Запускать в обновлённом контейнере (нужен
instagram.fetch_thread_full, возвращающий link_url/preview_url). Rate-limit + стоп."""
import asyncio
import random
import sys

from sqlalchemy import delete, or_, select

from stepan_shared.db.engine import get_session, init_engine
from stepan_shared.db.models import ChatRow, MessageRow
from ig_worker import instagram as ig
from ig_worker.common import to_naive_utc

CARD = ["🎬", "📷", "🔗", "📎", "📖", "👤", "🛍", "📅", "📱", "🖼", "🎤", "🏷"]


async def sync_chat(cid, tid, me):
    msgs = await ig.fetch_thread_full(tid)
    if not msgs:
        return 0, 0, 0
    ig_ids = {m["ig_message_id"] for m in msgs}
    oldest = min(to_naive_utc(m["timestamp"]) for m in msgs)
    added = updated = 0
    async with get_session() as s:
        rows = (await s.execute(select(MessageRow).where(MessageRow.chat_id == cid))).scalars().all()
        by_id = {r.ig_message_id: r for r in rows}
        for m in msgs:
            r = by_id.get(m["ig_message_id"])
            if r is None:
                inc = m["user_id"] != me
                s.add(MessageRow(chat_id=cid, direction="in" if inc else "out",
                                 ig_message_id=m["ig_message_id"], text=m["text"],
                                 sent_by="lead" if inc else "agent",
                                 link_url=m.get("link_url"), preview_url=m.get("preview_url"),
                                 occurred_at=to_naive_utc(m["timestamp"])))
                added += 1
            else:
                ch = False
                if r.text != m["text"]:
                    r.text = m["text"]; ch = True
                if (r.link_url or None) != (m.get("link_url") or None):
                    r.link_url = m.get("link_url"); ch = True
                if (r.preview_url or None) != (m.get("preview_url") or None):
                    r.preview_url = m.get("preview_url"); ch = True
                if ch:
                    updated += 1
        await s.flush()
        res = await s.execute(delete(MessageRow).where(
            MessageRow.chat_id == cid, MessageRow.occurred_at >= oldest,
            MessageRow.ig_message_id.notin_(ig_ids)))
        removed = res.rowcount or 0
        rows2 = (await s.execute(select(MessageRow.id, MessageRow.direction, MessageRow.occurred_at)
                                 .where(MessageRow.chat_id == cid).order_by(MessageRow.occurred_at))).all()
        c = await s.get(ChatRow, cid)
        ins = [r for r in rows2 if r[1] == "in"]
        outs = [r for r in rows2 if r[1] == "out"]
        c.last_in_at, c.last_in_msg_id = (ins[-1][2], ins[-1][0]) if ins else (None, None)
        c.last_out_at, c.last_out_msg_id = (outs[-1][2], outs[-1][0]) if outs else (None, None)
    return added, updated, removed


async def main():
    await init_engine()
    me = await ig.my_user_id()
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    async with get_session() as s:
        if arg and arg.isdigit():
            chats = (await s.execute(select(ChatRow.id, ChatRow.ig_thread_id).where(ChatRow.id == int(arg)))).all()
        else:
            conds = [MessageRow.text.like(f"{e}%") for e in CARD] + [MessageRow.text.like("[%]")]
            sub = select(MessageRow.chat_id).where(or_(*conds)).distinct()
            chats = (await s.execute(select(ChatRow.id, ChatRow.ig_thread_id)
                                     .where(ChatRow.id.in_(sub)).order_by(ChatRow.id.desc()))).all()
    print(f"me={me} chats={len(chats)} mode={arg or 'cards'}", flush=True)
    A = U = R = E = D = 0
    for cid, tid in chats:
        D += 1
        try:
            a, u, r = await sync_chat(cid, tid, me)
            A += a; U += u; R += r
            if a or u or r:
                print(f"  chat {cid}: +{a} ~{u} -{r}", flush=True)
        except ig.AccountBlocked as e:
            print(f"BLOCKED at {cid}: {str(e)[:60]} STOP", flush=True)
            break
        except Exception as e:
            E += 1
            print(f"  chat {cid} ERR: {str(e)[:80]}", flush=True)
        if D % 25 == 0:
            print(f"... {D}/{len(chats)} +{A} ~{U} -{R} e{E}", flush=True)
        if not (arg and arg.isdigit()):
            await asyncio.sleep(random.uniform(2.5, 4.5))
    print(f"DONE: processed={D} added={A} updated={U} removed={R} errs={E}", flush=True)


asyncio.run(main())
