"""Репозиторий агентов хаба и журнала вызовов."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_hub import HubAgentRow, HubCallRow
from vera_shared.hub.names import HubNameError, normalize_allow, valid_agent_name
from vera_shared.hub.secrets import new_agent_token, token_hash
from vera_shared.timeutil import utc_naive_now


async def list_agents() -> list[HubAgentRow]:
    async with get_session() as session:
        return list((await session.execute(select(HubAgentRow).order_by(HubAgentRow.name))).scalars())


async def create_agent(name: str, allow: list[str], note: str = "") -> str:
    """Возвращает токен — единственный раз; в БД остаётся только его sha256."""
    if not valid_agent_name(name):
        raise HubNameError("agent name: 2-32 chars a-z0-9_- starting with a letter")
    grants = normalize_allow(allow)
    token = new_agent_token()
    async with get_session() as session:
        if (await session.execute(select(HubAgentRow.id).where(HubAgentRow.name == name))).first():
            raise HubNameError(f"agent {name!r} already exists")
        session.add(HubAgentRow(name=name, token_hash=token_hash(token), token_prefix=token[:9],
                                allow=grants, note=note))
        await session.commit()
    return token


async def update_agent(agent_id: int, allow: list[str], enabled: bool, note: str) -> None:
    grants = normalize_allow(allow)
    async with get_session() as session:
        row = await session.get(HubAgentRow, agent_id)
        if row is None:
            raise LookupError(f"agent {agent_id} not found")
        row.allow, row.enabled, row.note = grants, enabled, note
        await session.commit()


async def rotate_agent(agent_id: int) -> str:
    token = new_agent_token()
    async with get_session() as session:
        row = await session.get(HubAgentRow, agent_id)
        if row is None:
            raise LookupError(f"agent {agent_id} not found")
        row.token_hash, row.token_prefix = token_hash(token), token[:9]
        await session.commit()
    return token


async def agent_by_token(token: str) -> HubAgentRow | None:
    """Включённый агент по токену; сравнение идёт по sha256 в индексе."""
    async with get_session() as session:
        found = await session.execute(select(HubAgentRow).where(
            HubAgentRow.token_hash == token_hash(token), HubAgentRow.enabled.is_(True)))
        return found.scalar_one_or_none()


async def touch_agent(agent_id: int) -> None:
    async with get_session() as session:
        row = await session.get(HubAgentRow, agent_id)
        if row is not None:
            row.last_seen_at = utc_naive_now()
            await session.commit()


async def insert_call(fields: dict[str, Any]) -> None:
    async with get_session() as session:
        session.add(HubCallRow(**fields))
        await session.commit()


async def recent_calls(limit: int = 100) -> list[HubCallRow]:
    async with get_session() as session:
        rows = await session.execute(
            select(HubCallRow).order_by(HubCallRow.id.desc()).limit(limit))
        return list(rows.scalars())
