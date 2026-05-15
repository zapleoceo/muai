from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Chat


class ChatRepo:
    async def upsert_chat(self, chat) -> None:
        first = getattr(chat, "first_name", None)
        last = getattr(chat, "last_name", None)
        if first is not None:
            title = " ".join(p for p in (first, last) if p) or first
        else:
            title = getattr(chat, "title", None)
        await self.upsert_chat_raw(
            id=chat.id,
            type=str(chat.type.value) if hasattr(chat.type, "value") else str(chat.type),
            title=title,
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
