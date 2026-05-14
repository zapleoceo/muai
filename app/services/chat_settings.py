from app.services.chat_query_service import ChatQueryService
from app.services.chat_sync_config_service import ChatSyncConfigService
from app.services.chat_sync_settings_service import ChatSyncSettingsService

_settings = ChatSyncSettingsService()
_config = ChatSyncConfigService()
_query = ChatQueryService()


async def get_global_settings() -> dict:
    return await _settings.get()


async def update_global_settings(patch: dict) -> dict:
    return await _settings.update(patch)


async def get_chat_config(chat_id: int):
    return await _config.get_chat_config(chat_id)


async def create_pending(chat_id: int) -> None:
    await _config.create_pending(chat_id)


async def approve_chat(chat_id: int, depth_days: int | None = None) -> None:
    await _config.approve_chat(chat_id, depth_days)


async def disable_chat(chat_id: int, reason: str = "") -> None:
    await _config.disable_chat(chat_id, reason)


async def is_blacklisted(chat_id: int, username: str | None, settings: dict) -> bool:
    return _settings.is_blacklisted(chat_id, username, settings)


async def type_allowed(chat_type: str, settings: dict) -> bool:
    return _settings.type_allowed(chat_type, settings)


async def list_chats_with_config() -> list[dict]:
    return await _query.list_chats_with_config()


async def update_chat_depth(chat_id: int, depth_days: int | None) -> None:
    await _config.update_chat_depth(chat_id, depth_days)


async def delete_chat_messages(chat_id: int) -> int:
    return await _query.delete_chat_messages(chat_id)


async def auto_approve_existing_chats() -> int:
    return await _config.auto_approve_existing_chats()


async def approve_all_pending(types: list[str] | None = None, depth_days: int | None = None) -> int:
    return await _config.approve_all_pending(types=types, depth_days=depth_days)
