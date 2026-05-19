import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.internal.agent_repo import get_agent_url

log = logging.getLogger(__name__)
_TIMEOUT = 60.0


@dataclass
class AgentResult:
    agent_id: str
    success: bool
    summary: str
    data: dict | list | None = None
    error: str | None = None


async def _call_agent(
    client: httpx.AsyncClient,
    agent_id: str,
    request: str,
    intent: dict,
    task_id: int | None,
) -> AgentResult:
    url = await get_agent_url(agent_id)
    if url is None:
        return AgentResult(agent_id, False, f"agent {agent_id} not registered", error="not_registered")
    try:
        resp = await client.post(
            f"{url}/task",
            json={"request": request, "intent": intent, "task_id": task_id},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        d = resp.json()
        return AgentResult(
            agent_id=agent_id,
            success=bool(d.get("success", False)),
            summary=str(d.get("summary", "")),
            data=d.get("data"),
            error=d.get("error"),
        )
    except httpx.TimeoutException:
        log.warning("Agent %s timed out", agent_id)
        return AgentResult(agent_id, False, f"таймаут {_TIMEOUT}s", error="timeout")
    except Exception as exc:
        log.warning("Agent %s error: %s", agent_id, exc)
        return AgentResult(agent_id, False, f"ошибка: {exc}", error=str(exc))


async def dispatch(
    agent_ids: list[str],
    request: str,
    intent: dict,
    task_id: int | None = None,
) -> list[AgentResult]:
    if not agent_ids:
        return []
    async with httpx.AsyncClient() as client:
        return await asyncio.gather(*[
            _call_agent(client, aid, request, intent, task_id) for aid in agent_ids
        ])
