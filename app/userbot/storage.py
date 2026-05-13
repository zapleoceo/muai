async def save_history_message(
    *,
    chat_id: int,
    chat_entity,
    msg,
    user_cache: dict[int, bool],
) -> bool:
    from app.services.message_ingest import ingest_telethon_history_message
    return await ingest_telethon_history_message(chat_id=chat_id, msg=msg, user_cache=user_cache)
