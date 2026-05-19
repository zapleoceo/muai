import json
import logging
from dataclasses import dataclass, field

from app.internal.agent_repo import get_online_agents

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are an intent classifier for a multi-agent AI system. "
    "Given a user request, return a JSON object with exactly these keys:\n"
    '  "summary": one-line restatement of the task,\n'
    '  "target_agents": list of agent IDs best suited to handle the task,\n'
    '  "context": dict of extra data useful for agents.\n'
    "Return only valid JSON, no explanation."
)


@dataclass
class Intent:
    summary: str
    target_agents: list[str]
    context: dict = field(default_factory=dict)


async def prefilter(text: str) -> Intent:
    agents = await get_online_agents()
    agent_list = ", ".join(a["id"] for a in agents) if agents else "none"

    prompt = (
        f"Available agents: [{agent_list}]\n\n"
        f"User request: {text}\n\n"
        "Classify the intent and choose the best agents."
    )

    try:
        from vera_shared.providers.registry import get_registry
        registry = get_registry()
        raw, _, _ = await registry.chat("prefilter", [{"role": "user", "content": prompt}], system=_SYSTEM)
        data = json.loads(raw)
        return Intent(
            summary=data.get("summary", text[:80]),
            target_agents=data.get("target_agents", []),
            context=data.get("context", {}),
        )
    except Exception as exc:
        log.warning("Prefilter failed, using fallback: %s", exc)
        return Intent(
            summary=text[:80],
            target_agents=["vera-core-fallback"] if not agents else [agents[0]["id"]],
            context={},
        )
