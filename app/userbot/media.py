from telethon.tl.types import Channel, Chat, User


def chat_type(entity) -> str:
    if isinstance(entity, User):
        return "private"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "supergroup" if entity.megagroup else "channel"
    return "unknown"


def chat_title(entity) -> str | None:
    if isinstance(entity, User):
        return entity.first_name
    return getattr(entity, "title", None)


def chat_username(entity) -> str | None:
    return getattr(entity, "username", None)


def media_type(msg) -> str | None:
    if msg.photo:       return "photo"
    if msg.voice:       return "voice"
    if msg.video:       return "video"
    if msg.document:    return "document"
    if msg.sticker:     return "sticker"
    if msg.audio:       return "audio"
    if getattr(msg, "poll",       None): return "poll"
    if getattr(msg, "geo",        None): return "location"
    if getattr(msg, "contact",    None): return "contact"
    if getattr(msg, "dice",       None): return "dice"
    if getattr(msg, "game",       None): return "game"
    if getattr(msg, "video_note", None): return "video_note"
    if getattr(msg, "gif",        None): return "animation"
    return None
