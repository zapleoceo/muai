from telethon.tl.functions.contacts import SearchRequest
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
    client = get_client()
    seen: set[int] = set()
    results: list[dict] = []

    for q in _query_variants(query):
        try:
            res = await client(SearchRequest(q=q, limit=limit))
        except Exception:
            continue
        for e in list(res.users) + list(res.chats):
            if e.id in seen:
                continue
            seen.add(e.id)
            results.append({
                "id": e.id,
                "name": _name(e),
                "type": _type(e),
                "username": getattr(e, "username", None),
            })
    return results[:limit]
