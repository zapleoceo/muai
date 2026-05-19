from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Agent


async def get_online_agents() -> list[dict]:
    async with get_session() as session:
        result = await session.execute(select(Agent).where(Agent.status == "online"))
        return [
            {"id": a.id, "http_url": a.http_url, "capabilities": a.capabilities}
            for a in result.scalars().all()
        ]


async def get_agent_url(agent_id: str) -> str | None:
    async with get_session() as session:
        agent = await session.get(Agent, agent_id)
        return agent.http_url if agent else None


async def list_all_agents() -> list[dict]:
    async with get_session() as session:
        result = await session.execute(select(Agent))
        return [
            {
                "id": a.id, "name": a.name, "http_url": a.http_url,
                "capabilities": a.capabilities, "status": a.status,
                "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
            }
            for a in result.scalars().all()
        ]
