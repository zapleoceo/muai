def build_message_link(
    *,
    chat_id: int,
    chat_type: str | None,
    chat_username: str | None,
    telegram_msg_id: int | None,
) -> str | None:
    if not telegram_msg_id:
        return None
    if chat_username:
        u = chat_username.lstrip("@")
        return f"https://t.me/{u}/{telegram_msg_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{telegram_msg_id}"
    if chat_type in ("group", "supergroup", "channel") and chat_id < 0:
        return f"https://t.me/c/{abs(chat_id)}/{telegram_msg_id}"
    return None
