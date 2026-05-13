from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Setting


class SettingsRepo:
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = (await self.session.execute(
            select(Setting).where(Setting.key == key)
        )).scalar_one_or_none()
        return row.value if row else default

    async def set_setting(self, key: str, value: str) -> None:
        stmt = (
            insert(Setting)
            .values(key=key, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
        await self.session.execute(stmt)
