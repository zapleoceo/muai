"""Репозиторий апстримов хаба. Секрет попадает сюда открытым только на запись."""
from __future__ import annotations

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_hub import HubUpstreamRow
from vera_shared.hub.secrets import seal
from vera_shared.hub.spec import UpstreamSpec
from vera_shared.timeutil import utc_naive_now


async def list_upstreams() -> list[HubUpstreamRow]:
    async with get_session() as session:
        rows = await session.execute(
            select(HubUpstreamRow).order_by(HubUpstreamRow.project, HubUpstreamRow.slug))
        return list(rows.scalars())


async def save_upstream(spec: UpstreamSpec, upstream_id: int | None = None,
                        secret: str | None = None, clear_secret: bool = False) -> int:
    """Создать или обновить. `secret=None` — оставить прежний; пустая строка — тоже."""
    spec.validate()
    async with get_session() as session:
        row = await session.get(HubUpstreamRow, upstream_id) if upstream_id else None
        if upstream_id and row is None:
            raise LookupError(f"upstream {upstream_id} not found")
        if row is None:
            row = HubUpstreamRow(project=spec.project, slug=spec.slug)
            session.add(row)
        for key in ("project", "slug", "title", "transport", "url", "command", "auth_kind",
                    "auth_header", "enabled", "hint", "timeout_s", "max_concurrency"):
            setattr(row, key, getattr(spec, key))
        if clear_secret or spec.auth_kind == "none":
            row.secret_enc = None
        elif secret:
            row.secret_enc = seal(secret)
        row.updated_at = utc_naive_now()
        await session.flush()
        new_id = row.id
        await session.commit()
        return new_id


async def set_health(upstream_id: int, health: str, error: str = "",
                     tool_count: int | None = None) -> None:
    async with get_session() as session:
        row = await session.get(HubUpstreamRow, upstream_id)
        if row is None:
            return
        row.health, row.health_error, row.health_at = health, error[:200], utc_naive_now()
        if tool_count is not None:
            row.tool_count = tool_count
        await session.commit()
