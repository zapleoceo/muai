import json
import logging
from dataclasses import dataclass, field

from app.internal.agent_repo import get_online_agents

log = logging.getLogger(__name__)

_SYSTEM = """You are an intent classifier for a multi-agent AI system.

Return ONLY a JSON object with these exact keys:
  "summary": one-line restatement of the task
  "target_agents": list of agent IDs from the provided list that should handle it
  "context": dict of extra useful data (empty object if none)

Rules:
- Use target_agents=[] for: greetings, small talk, meta questions ("кто ты",
  "что умеешь"), thanks, general knowledge questions, jokes, anything that
  does NOT require external integrations. The orchestrator will answer itself.
- Use a Telegram agent only if the task explicitly involves reading/searching/
  sending Telegram chats, channels, or messages.
- Never invent agent IDs. Only choose from the provided list.

Examples:
  "привет" → {"summary":"greeting","target_agents":[],"context":{}}
  "кто ты, что умеешь" → {"summary":"self-introduction","target_agents":[],"context":{}}
  "расскажи анекдот" → {"summary":"joke","target_agents":[],"context":{}}
  "какая столица Франции" → {"summary":"trivia","target_agents":[],"context":{}}
  "прочитай последние 20 сообщений из чата Лиза" →
    {"summary":"read last 20 msgs from Лиза","target_agents":["vera-telegram"],
     "context":{"peer":"Лиза","limit":20}}
  "найди чат про крипту" →
    {"summary":"search Telegram dialogs","target_agents":["vera-telegram"],
     "context":{"query":"крипта"}}
"""


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
        "Classify and return JSON."
    )

    try:
        from vera_shared.providers.registry import get_registry
        registry = get_registry()
        raw, _, _ = await registry.chat("prefilter", [{"role": "user", "content": prompt}], system=_SYSTEM)
        data = json.loads(_strip_code_fence(raw))
        return Intent(
            summary=data.get("summary", text[:80]),
            target_agents=data.get("target_agents", []) or [],
            context=data.get("context", {}) or {},
        )
    except Exception as exc:
        log.warning("Prefilter failed, defaulting to self-answer: %s", exc)
        return Intent(summary=text[:80], target_agents=[], context={})


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()
