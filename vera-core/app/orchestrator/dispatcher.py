import asyncio
import logging

import httpx

from app.internal.agent_repo import get_agent_url

log = logging.getLogger(__name__)
_TIMEOUT = 30.0


async def _call_agent(client: httpx.AsyncClient, agent_id: str, prompt: str) -> tuple[str, str]:
    url = await get_agent_url(agent_id)
    if url is None:
        return agent_id, f"ERROR: agent {agent_id!r} not registered"
    try:
        resp = await client.post(
            f"{url}/task",
            json={"prompt": prompt},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return agent_id, data.get("result", "")
    except httpx.TimeoutException:
        log.warning("Agent %s timed out", agent_id)
        return agent_id, f"ERROR: timeout after {_TIMEOUT}s"
    except Exception as exc:
        log.warning("Agent %s error: %s", agent_id, exc)
        return agent_id, f"ERROR: {exc}"


async def dispatch(agent_ids: list[str], prompts: dict[str, str]) -> dict[str, str]:
    if not agent_ids:
        return {}

    async with httpx.AsyncClient() as client:
        tasks = [_call_agent(client, aid, prompts.get(aid, "")) for aid in agent_ids]
        pairs = await asyncio.gather(*tasks)

    return dict(pairs)
