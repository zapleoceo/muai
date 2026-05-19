from datetime import datetime

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Agent
from vera_shared.registry.model import AgentRecord


def _to_record(row: Agent) -> AgentRecord:
    return AgentRecord(
        id=row.id,
        name=row.name,
        capabilities=row.capabilities or [],
        required_caps=row.required_caps or [],
        http_url=row.http_url,
        bot_username=row.bot_username,
        status=row.status,  # type: ignore[arg-type]
        last_heartbeat=row.last_heartbeat,
        registered_at=row.registered_at,
    )


async def upsert_agent(record: AgentRecord) -> None:
    async with get_session() as session:
        row = await session.get(Agent, record.id)
        if row is None:
            row = Agent(id=record.id)
            session.add(row)
        row.name = record.name
        row.capabilities = record.capabilities
        row.required_caps = record.required_caps
        row.http_url = record.http_url
        row.bot_username = record.bot_username
        row.status = record.status
        row.registered_at = record.registered_at
        await session.commit()


async def get_agent(agent_id: str) -> AgentRecord | None:
    async with get_session() as session:
        row = await session.get(Agent, agent_id)
        return _to_record(row) if row else None


async def get_online_agents() -> list[AgentRecord]:
    async with get_session() as session:
        result = await session.execute(select(Agent).where(Agent.status == "online"))
        return [_to_record(r) for r in result.scalars().all()]


async def get_agents_by_capability(cap: str) -> list[AgentRecord]:
    agents = await get_online_agents()
    return [a for a in agents if cap in a.capabilities]


async def update_status(agent_id: str, status: str) -> None:
    async with get_session() as session:
        row = await session.get(Agent, agent_id)
        if row:
            row.status = status
            await session.commit()


async def update_heartbeat(agent_id: str) -> None:
    async with get_session() as session:
        row = await session.get(Agent, agent_id)
        if row:
            row.last_heartbeat = datetime.utcnow()
            row.status = "online"
            await session.commit()
