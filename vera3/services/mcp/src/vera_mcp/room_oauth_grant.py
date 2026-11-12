"""Room OAuth grant primitives: hashed storage key, expiry and liveness."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from vera_mcp.oauth_models import RoomOAuthGrant


def grant_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def expiry_after(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def grant_alive(row: RoomOAuthGrant | None) -> bool:
    if row is None:
        return False
    dt = row.expires_at
    if dt.tzinfo is None:  # SQLite in unit tests
        dt = dt.replace(tzinfo=UTC)
    return dt > datetime.now(UTC)
