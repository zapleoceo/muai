from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, ChatSyncConfig


class ChatSyncConfigService:
    async def get_chat_config(self, chat_id: int) -> ChatSyncConfig | None:
        async with AsyncSessionLocal() as session:
            return (await session.execute(
                select(ChatSyncConfig).where(ChatSyncConfig.chat_id == chat_id)
            )).scalar_one_or_none()

    async def create_pending(self, chat_id: int) -> None:
        async with AsyncSessionLocal() as session:
            stmt = (
                insert(ChatSyncConfig)
                .values(chat_id=chat_id, enabled=False, approved_at=None)
                .on_conflict_do_nothing(index_elements=["chat_id"])
            )
            await session.execute(stmt)
            await session.commit()

    async def approve_chat(self, chat_id: int, depth_days: int | None = None) -> None:
        async with AsyncSessionLocal() as session:
            now = datetime.now(tz=timezone.utc)
            stmt = (
                insert(ChatSyncConfig)
                .values(
                    chat_id=chat_id,
                    enabled=True,
                    approved_at=now,
                    depth_days=depth_days,
                    skip_reason=None,
                )
                .on_conflict_do_update(
                    index_elements=["chat_id"],
                    set_={
                        "enabled": True,
                        "approved_at": now,
                        "depth_days": depth_days,
                        "skip_reason": None,
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def disable_chat(self, chat_id: int, reason: str = "") -> None:
        async with AsyncSessionLocal() as session:
            stmt = (
                insert(ChatSyncConfig)
                .values(chat_id=chat_id, enabled=False, skip_reason=reason)
                .on_conflict_do_update(
                    index_elements=["chat_id"],
                    set_={"enabled": False, "skip_reason": reason},
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def update_chat_depth(self, chat_id: int, depth_days: int | None) -> None:
        async with AsyncSessionLocal() as session:
            stmt = (
                insert(ChatSyncConfig)
                .values(chat_id=chat_id, depth_days=depth_days)
                .on_conflict_do_update(index_elements=["chat_id"], set_={"depth_days": depth_days})
            )
            await session.execute(stmt)
            await session.commit()

    async def approve_all_pending(self, types: list[str] | None = None, depth_days: int | None = None) -> int:
        now = datetime.now(tz=timezone.utc)
        async with AsyncSessionLocal() as session:
            q = (
                select(ChatSyncConfig.chat_id)
                .join(Chat, Chat.id == ChatSyncConfig.chat_id)
                .where(ChatSyncConfig.enabled.is_(False))
                .where(ChatSyncConfig.approved_at.is_(None))
            )
            if types:
                q = q.where(Chat.type.in_(types))
            ids = (await session.execute(q)).scalars().all()
            if not ids:
                return 0
            await session.execute(
                update(ChatSyncConfig)
                .where(ChatSyncConfig.chat_id.in_(ids))
                .values(enabled=True, approved_at=now, depth_days=depth_days, skip_reason=None)
            )
            await session.commit()
            return len(ids)

    async def auto_approve_existing_chats(self) -> int:
        async with AsyncSessionLocal() as session:
            existing_ids = (await session.execute(
                select(Chat.id).where(
                    ~Chat.id.in_(select(ChatSyncConfig.chat_id))
                )
            )).scalars().all()

            if not existing_ids:
                return 0

            now = datetime.now(tz=timezone.utc)
            await session.execute(
                insert(ChatSyncConfig).values([
                    {"chat_id": cid, "enabled": True, "approved_at": now}
                    for cid in existing_ids
                ]).on_conflict_do_nothing()
            )
            await session.commit()
            return len(existing_ids)

