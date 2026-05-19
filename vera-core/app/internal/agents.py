import logging
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from vera_shared.db.engine import get_session
from vera_shared.db.models import Agent

from app.internal.agent_repo import list_all_agents

log = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/agents")


class RegisterPayload(BaseModel):
    id: str
    name: str
    http_url: str
    capabilities: list[str] = []
    required_caps: list[str] = []
    bot_username: str | None = None
    tools: list[dict] = []


class HeartbeatPayload(BaseModel):
    id: str


@router.post("/register")
async def register_agent(payload: RegisterPayload) -> dict:
    async with get_session() as session:
        existing = await session.get(Agent, payload.id)
        if existing:
            existing.name = payload.name
            existing.http_url = payload.http_url
            existing.capabilities = payload.capabilities
            existing.required_caps = payload.required_caps
            existing.bot_username = payload.bot_username
            existing.tools = payload.tools
            existing.status = "online"
            existing.last_heartbeat = datetime.utcnow()
        else:
            session.add(Agent(
                id=payload.id, name=payload.name, http_url=payload.http_url,
                capabilities=payload.capabilities, required_caps=payload.required_caps,
                bot_username=payload.bot_username, tools=payload.tools,
                status="online", last_heartbeat=datetime.utcnow(),
            ))
        await session.commit()
    return {"ok": True}


@router.post("/heartbeat")
async def heartbeat(payload: HeartbeatPayload) -> dict:
    async with get_session() as session:
        agent = await session.get(Agent, payload.id)
        if agent:
            agent.last_heartbeat = datetime.utcnow()
            agent.status = "online"
            await session.commit()
    return {"ok": True}


@router.get("")
async def list_agents() -> list[dict]:
    return await list_all_agents()
