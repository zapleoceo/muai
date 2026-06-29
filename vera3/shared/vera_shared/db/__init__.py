"""SQLAlchemy DB layer — Postgres + pgvector."""
from vera_shared.db.engine import (
    AsyncSessionLocal,
    Base,
    get_engine,
    get_session,
    init_engine,
)
from vera_shared.db.models import (
    EventRow,
    JobRow,
    SourceRow,
    UsageLogRow,
)
from vera_shared.db.models_sources import GmailAccountRow, TelegramSessionRow

__all__ = [
    "Base",
    "AsyncSessionLocal",
    "get_engine",
    "get_session",
    "init_engine",
    "EventRow",
    "SourceRow",
    "JobRow",
    "UsageLogRow",
    "GmailAccountRow",
    "TelegramSessionRow",
]
