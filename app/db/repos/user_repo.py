from sqlalchemy.dialects.postgresql import insert

from app.db.models import TgUser


class UserRepo:
    async def upsert_user(self, user) -> None:
        await self.upsert_user_raw(
            id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=getattr(user, "language_code", None),
            is_bot=user.is_bot,
        )

    async def upsert_user_raw(
        self,
        *,
        id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        is_bot: bool = False,
    ) -> None:
        stmt = (
            insert(TgUser)
            .values(
                id=id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                language_code=language_code,
                is_bot=is_bot,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"username": username, "first_name": first_name, "last_name": last_name},
            )
        )
        await self.session.execute(stmt)
