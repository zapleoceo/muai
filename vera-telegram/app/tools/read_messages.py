from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat, User

from app.userbot.client import get_client

_RU_TO_EN = str.maketrans({
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo","ж":"zh","з":"z",
    "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
    "с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch",
    "ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
})
_EN_TO_RU_PAIRS = [
    ("sch","щ"),("sh","ш"),("ch","ч"),("yo","ё"),("yu","ю"),("ya","я"),
    ("zh","ж"),("kh","х"),
]
_EN_TO_RU_SINGLE = str.maketrans({
    "a":"а","b":"б","v":"в","g":"г","d":"д","e":"е","z":"з","i":"и","y":"й",
    "k":"к","l":"л","m":"м","n":"н","o":"о","p":"п","r":"р","s":"с","t":"т",
    "u":"у","f":"ф","h":"х","c":"ц",
})


def _translit_ru_to_en(s: str) -> str:
    return s.lower().translate(_RU_TO_EN)


def _translit_en_to_ru(s: str) -> str:
    s = s.lower()
    for en, ru in _EN_TO_RU_PAIRS:
        s = s.replace(en, ru)
    return s.translate(_EN_TO_RU_SINGLE)


def _query_variants(peer: str) -> list[str]:
    p = peer.lower()
    has_cyr = any("а" <= c <= "я" or c == "ё" for c in p)
    has_lat = any("a" <= c <= "z" for c in p)
    out = {p}
    if has_cyr and not has_lat:
        out.add(_translit_ru_to_en(p))
    if has_lat and not has_cyr:
        out.add(_translit_en_to_ru(p))
    return [v for v in out if v]


def _sender_name(msg) -> str:
    sender = getattr(msg, "_sender", None) or getattr(msg, "sender", None)
    if sender is None:
        return "unknown"
    title = getattr(sender, "title", None)
    if title:
        return title
    fn = getattr(sender, "first_name", None) or ""
    ln = getattr(sender, "last_name", None) or ""
    return " ".join(filter(None, [fn, ln])) or str(getattr(sender, "id", ""))


def _entity_name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    fn = getattr(entity, "first_name", None) or ""
    ln = getattr(entity, "last_name", None) or ""
    return " ".join(filter(None, [fn, ln])) or str(getattr(entity, "id", ""))


def _entity_type(entity) -> str:
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "supergroup"
    if isinstance(entity, Chat):
        return "group"
    return "unknown"


_ANNOUNCEMENT_HINTS = (
    "анонс", "новост", "объявл", "афиш", "событ",
    "announcement", "news", "event",
)


def _is_announcement_intent(request: str) -> bool:
    r = request.lower()
    return any(h in r for h in _ANNOUNCEMENT_HINTS)


def _rank_candidate(entity, last_date, prefer_broadcast: bool) -> tuple:
    t = _entity_type(entity)
    type_score = {
        "channel": 3 if prefer_broadcast else 1,
        "supergroup": 2 if prefer_broadcast else 2,
        "group": 1 if prefer_broadcast else 2,
        "user": 0 if prefer_broadcast else 3,
        "bot": -1,
    }.get(t, 0)
    ts = last_date.timestamp() if last_date else 0
    return (type_score, ts)


async def _candidates_from_recent(queries: list[str], dialogs_limit: int = 300) -> list[tuple]:
    client = get_client()
    matches: list[tuple] = []
    try:
        async for d in client.iter_dialogs(limit=dialogs_limit):
            name = _entity_name(d.entity).lower()
            if any(q in name for q in queries):
                matches.append((d.entity, d.date))
    except FloodWaitError:
        pass
    return matches


async def _candidates_from_search(queries: list[str], per_query: int = 10) -> list[tuple]:
    client = get_client()
    seen: set[int] = set()
    matches: list[tuple] = []
    for q in queries:
        try:
            res = await client(SearchRequest(q=q, limit=per_query))
        except Exception:
            continue
        for e in list(res.users) + list(res.chats):
            if e.id not in seen:
                seen.add(e.id)
                matches.append((e, None))
    return matches


async def _read_one(entity, limit: int, cutoff: datetime) -> list[dict]:
    client = get_client()
    out: list[dict] = []
    try:
        async for msg in client.iter_messages(entity, limit=limit):
            if msg.date and msg.date < cutoff:
                break
            await msg.get_sender()
            out.append({
                "id": msg.id,
                "date": msg.date.isoformat() if msg.date else None,
                "text": msg.text or "",
                "from": _sender_name(msg),
                "out": msg.out,
            })
    except Exception as exc:
        out.append({"_error": str(exc)})
    return out


async def read_messages(peer: str, limit: int = 50, offset_days: int = 1,
                        request_hint: str = "") -> dict:
    if not peer:
        raise LookupError("peer пустой — укажи с кем читать переписку")

    client = get_client()
    cutoff = datetime.now(timezone.utc) - timedelta(days=offset_days)
    queries = _query_variants(peer)
    prefer_broadcast = _is_announcement_intent(request_hint)

    # Path A: numeric ID or exact handle
    if peer.lstrip("-").isdigit():
        entity = await client.get_entity(int(peer))
        msgs = await _read_one(entity, limit, cutoff)
        return _pack([(entity, msgs)], 1, queries, prefer_broadcast)

    # Path B: dedup candidates from recent dialogs + server search
    recent = await _candidates_from_recent(queries)
    server = await _candidates_from_search(queries)

    by_id: dict[int, tuple] = {}
    for e, dt in recent + server:
        if e.id not in by_id:
            by_id[e.id] = (e, dt)

    if not by_id:
        raise LookupError(
            f"диалог с «{peer}» не найден (искал также: {', '.join(queries)})"
        )

    ranked = sorted(
        by_id.values(),
        key=lambda x: _rank_candidate(x[0], x[1], prefer_broadcast),
        reverse=True,
    )

    top_n = ranked[:3]
    reads: list[tuple] = []
    for entity, _ in top_n:
        msgs = await _read_one(entity, limit, cutoff)
        if msgs and not (len(msgs) == 1 and "_error" in msgs[0]):
            reads.append((entity, msgs))

    if not reads:
        return _pack([(top_n[0][0], [])], len(by_id), queries, prefer_broadcast)

    return _pack(reads, len(by_id), queries, prefer_broadcast)


def _pack(reads: list[tuple], total_candidates: int, queries: list[str],
          prefer_broadcast: bool) -> dict:
    chats = []
    for entity, msgs in reads:
        chats.append({
            "chat_name": _entity_name(entity),
            "chat_id": entity.id,
            "chat_type": _entity_type(entity),
            "messages_count": sum(1 for m in msgs if "_error" not in m),
            "messages": msgs,
        })
    return {
        "queries_tried": queries,
        "candidates_total": total_candidates,
        "prefer_broadcast": prefer_broadcast,
        "chats_read": chats,
    }
