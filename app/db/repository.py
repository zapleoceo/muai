from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repos import ChatRepo, ChunkRepo, MessageOpsRepo, SettingsRepo, UserRepo


class MessageRepo(ChatRepo, UserRepo, MessageOpsRepo, ChunkRepo, SettingsRepo):
    def __init__(self, session: AsyncSession):
        self.session = session
