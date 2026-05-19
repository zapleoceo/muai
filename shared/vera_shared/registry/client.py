import asyncio

import httpx

import vera_shared.registry.repository as repo
from vera_shared.registry.model import AgentRecord

_HEARTBEAT_INTERVAL = 30


async def register_self(
    agent_id: str,
    name: str,
    capabilities: list[str],
    required_caps: list[str],
    http_url: str,
    bot_username: str | None,
    vera_core_url: str,
) -> None:
    record = AgentRecord(
        id=agent_id,
        name=name,
        capabilities=capabilities,
        required_caps=required_caps,
        http_url=http_url,
        bot_username=bot_username,
        status="online",
    )
    await repo.upsert_agent(record)

    payload = {
        "agent_id": agent_id,
        "name": name,
        "capabilities": capabilities,
        "required_caps": required_caps,
        "http_url": http_url,
        "bot_username": bot_username,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(f"{vera_core_url}/internal/agents/register", json=payload)
        except httpx.HTTPError:
            # vera-core may not be up yet; local DB registration still succeeded
            pass


async def heartbeat_loop(agent_id: str, vera_core_url: str) -> None:
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        await repo.update_heartbeat(agent_id)

        async with httpx.AsyncClient(timeout=5) as client:
            try:
                await client.post(
                    f"{vera_core_url}/internal/agents/heartbeat",
                    json={"agent_id": agent_id},
                )
            except httpx.HTTPError:
                pass
