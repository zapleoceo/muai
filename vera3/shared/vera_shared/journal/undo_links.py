"""Откат правок связей: прозвища (`nickname`) и карта голосов (`speaker`)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_mcp import McpAuditRow
from vera_shared.links.nicknames import restore_nickname
from vera_shared.links.speakers import put_speaker


async def undo_nickname(s: AsyncSession, row: McpAuditRow,
                        _force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """`before` — снимок до правки (None — прозвище создано ею), `after` — снимок после."""
    await restore_nickname(s, int(row.target_id or 0), row.before)
    return row.after or {}, row.before or {"exists": False}


async def undo_speaker(s: AsyncSession, row: McpAuditRow,
                       _force: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """target_id — событие; `before.entity_id` — кого называл ярлык раньше (None — никого)."""
    after = row.after or {}
    before = row.before or {}
    await put_speaker(s, int(row.target_id or 0), str(after["label"]), before.get("entity_id"))
    return after, before
