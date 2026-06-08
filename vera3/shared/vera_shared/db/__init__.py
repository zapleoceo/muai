"""SQLAlchemy DB layer — Postgres + pgvector."""
from vera_shared.db.engine import (
    Base,
    AsyncSessionLocal,
    get_engine,
    get_session,
    init_engine,
)
from vera_shared.db.models import (
    EventRow,
    TokenRow,
    SourceRow,
    JobRow,
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
    "TokenRow",
    "SourceRow",
    "JobRow",
    "UsageLogRow",
    "GmailAccountRow",
    "TelegramSessionRow",
]
