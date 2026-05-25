from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User

from app.userbot.client import get_client


_RU_TO_EN = str.maketrans({
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo","ж":"zh","з":"z",
    "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
    "с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch",
    "ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
})
_EN_TO_RU_PAIRS = [("sch","щ"),("sh","ш"),("ch","ч"),("yo","ё"),("yu","ю"),
                   ("ya","я"),("zh","ж"),("kh","х")]
_EN_TO_RU_SINGLE = str.maketrans({
    "a":"а","b":"б","v":"в","g":"г","d":"д","e":"е","z":"з","i":"и","y":"й",
    "k":"к","l":"л","m":"м","n":"н","o":"о","p":"п","r":"р","s":"с","t":"т",
    "u":"у","f":"ф","h":"х","c":"ц",
})


def _query_variants(s: str) -> list[str]:
    s = s.lower()
    out = {s}
    has_cyr = any("а" <= c <= "я" or c == "ё" for c in s)
    has_lat = any("a" <= c <= "z" for c in s)
    if has_cyr and not has_lat:
        out.add(s.translate(_RU_TO_EN))
    if has_lat and not has_cyr:
        t = s
        for en, ru in _EN_TO_RU_PAIRS:
            t = t.replace(en, ru)
        out.add(t.translate(_EN_TO_RU_SINGLE))
    return [v for v in out if v]


def _type(entity) -> str:
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "supergroup"
    if isinstance(entity, Chat):
        return "group"
    return "unknown"


def _name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    fn = getattr(entity, "first_name", None) or ""
    ln = getattr(entity, "last_name", None) or ""
    return " ".join(filter(None, [fn, ln])) or str(entity.id)


async def search_dialogs(query: str, limit: int = 15) -> list[dict]:
    """Search USER's own dialogs — by chat title AND folder name.
    Scans ALL dialogs (not first 500) so big accounts find everything.
    Also matches loose forms: case-insensitive, no-space, with translit."""
    client = get_client()
    variants = _expand_variants(query)
    out: list[dict] = []
    seen: set[int] = set()

    # 1) Build folder map: chat_id -> [folder_titles] (a chat may live in
    #    multiple folders).
    folder_map = await _folder_membership(client)
    matched_folders = {
        ft for ft in {f for fs in folder_map.values() for f in fs}
        if any(v in _norm(ft) for v in variants)
    }

    try:
        async for d in client.iter_dialogs():
            cid = d.entity.id
            if cid in seen:
                continue
            name_l = _norm(_name(d.entity))
            folder_titles = folder_map.get(cid, [])
            in_matched_folder = any(ft in matched_folders for ft in folder_titles)
            name_hit = any(v in name_l for v in variants)
            if not (name_hit or in_matched_folder):
                continue
            seen.add(cid)
            out.append({
                "id": cid,
                "name": _name(d.entity),
                "type": _type(d.entity),
                "username": getattr(d.entity, "username", None),
                "folders": folder_titles,
                "match": "folder" if in_matched_folder and not name_hit else "title",
                "unread_count": d.unread_count,
                "last_message_date": d.date.isoformat() if d.date else None,
            })
            if len(out) >= limit:
                break
    except FloodWaitError as exc:
        return [{"_error": f"flood wait {exc.seconds}s", "_partial": out}]

    out.sort(key=lambda x: x.get("last_message_date") or "", reverse=True)
    if not out:
        # Diagnostic — help LLM understand what to try next.
        return [{"_note": (
            f"no dialogs match {query!r} (variants tried: {variants}). "
            f"All folders: {sorted({f for fs in folder_map.values() for f in fs})}. "
            f"Try a different query or use telegram_list_folders."
        )}]
    return out


def _norm(s: str) -> str:
    """Lowercase + strip spaces/punct for loose matching."""
    return "".join(c for c in s.lower() if c.isalnum())


def _expand_variants(q: str) -> list[str]:
    base = _query_variants(q)
    out: set[str] = set()
    for b in base:
        out.add(_norm(b))
    return [v for v in out if v]


async def _folder_membership(client) -> dict[int, list[str]]:
    """Map peer_id -> list of folder titles it belongs to."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    out: dict[int, list[str]] = {}
    try:
        result = await client(GetDialogFiltersRequest())
        filters_list = getattr(result, "filters", None) or result
        for f in filters_list:
            t = getattr(f, "title", None)
            title = getattr(t, "text", t) if t else None
            if not title:
                continue
            for field in ("include_peers", "pinned_peers"):
                for p in getattr(f, field, None) or []:
                    pid = (getattr(p, "user_id", None)
                           or getattr(p, "chat_id", None)
                           or getattr(p, "channel_id", None))
                    if pid is not None:
                        out.setdefault(int(pid), []).append(title)
    except Exception:
        pass
    return out
