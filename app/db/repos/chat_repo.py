from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Chat


class ChatRepo:
    async def upsert_chat(self, chat) -> None:
        await self.upsert_chat_raw(
            id=chat.id,
            type=str(chat.type.value) if hasattr(chat.type, "value") else str(chat.type),
            title=getattr(chat, "title", None) or getattr(chat, "first_name", None),
            username=getattr(chat, "username", None),
        )

    async def upsert_chat_raw(
        self,
        *,
        id: int,
        type: str,
        title: str | None = None,
        username: str | None = None,
    ) -> None:
        stmt = (
            insert(Chat)
            .values(id=id, type=type, title=title, username=username)
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"type": type, "title": title, "username": username},
            )
        )
        await self.session.execute(stmt)

    async def list_all_chats(self) -> list[Chat]:
        q = select(Chat).order_by(Chat.title)
        return list((await self.session.execute(q)).scalars().all())
