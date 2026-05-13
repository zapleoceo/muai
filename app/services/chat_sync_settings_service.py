import json

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import Setting


class ChatSyncSettingsService:
    def __init__(self) -> None:
        self._key = "sync_settings"
        self._defaults: dict = {
            "allowed_types": ["private", "group", "supergroup", "channel"],
            "blacklist": [],
            "default_depth_days": 7,
        }

    async def get(self) -> dict:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(Setting).where(Setting.key == self._key)
            )).scalar_one_or_none()
        if row:
            try:
                return {**self._defaults, **json.loads(row.value)}
            except Exception:
                return dict(self._defaults)
        return dict(self._defaults)

    async def update(self, patch: dict) -> dict:
        current = await self.get()
        current.update(patch)
        async with AsyncSessionLocal() as session:
            from sqlalchemy.dialects.postgresql import insert
            stmt = (
                insert(Setting)
                .values(key=self._key, value=json.dumps(current))
                .on_conflict_do_update(index_elements=["key"], set_={"value": json.dumps(current)})
            )
            await session.execute(stmt)
            await session.commit()
        return current

    def is_blacklisted(self, chat_id: int, username: str | None, settings: dict) -> bool:
        bl = settings.get("blacklist", [])
        if chat_id in bl:
            return True
        if username and (username in bl or f"@{username}" in bl):
            return True
        return False

    def type_allowed(self, chat_type: str, settings: dict) -> bool:
        return chat_type in settings.get("allowed_types", self._defaults["allowed_types"])
