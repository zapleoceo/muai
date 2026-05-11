import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class SyncStatus:
    running: bool = False
    started_at: datetime | None = None
    current_chat: str | None = None
    chats_done: int = 0
    messages_saved: int = 0


class SyncManager:
    def __init__(self) -> None:
        self._cancelled: set[int] = set()
        self._task: asyncio.Task | None = None
        self.status: SyncStatus = SyncStatus()

    def cancel_chat(self, chat_id: int) -> None:
        self._cancelled.add(chat_id)
        logger.info("SyncManager: cancel requested for chat %d", chat_id)

    def is_cancelled(self, chat_id: int) -> bool:
        return chat_id in self._cancelled

    def clear_cancel(self, chat_id: int) -> None:
        self._cancelled.discard(chat_id)

    def stop_all(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("SyncManager: global sync task cancelled")

    def set_task(self, task: asyncio.Task) -> None:
        self._task = task

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def mark_started(self) -> None:
        self.status = SyncStatus(running=True, started_at=datetime.now(tz=timezone.utc))

    def mark_done(self) -> None:
        self.status.running = False
        self.status.current_chat = None

    def update_progress(self, chat_name: str, chats_done: int, messages_saved: int) -> None:
        self.status.current_chat = chat_name
        self.status.chats_done = chats_done
        self.status.messages_saved = messages_saved


_manager: SyncManager | None = None


def get_sync_manager() -> SyncManager:
    global _manager
    if _manager is None:
        _manager = SyncManager()
    return _manager
