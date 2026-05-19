import json
import logging
from dataclasses import dataclass, field

from app.internal.agent_repo import get_online_agents

log = logging.getLogger(__name__)

_SYSTEM = """You are an intent classifier for a multi-agent AI system named Vera.

Return ONLY a JSON object:
  "summary": one-line restatement of the user's intent (in Russian)
  "target_agents": list of agent IDs from the provided list (or [] if Vera answers herself)
  "context": dict with structured params the agent needs

Rules:
- target_agents=[] for greetings, small talk, "кто ты / что умеешь",
  general knowledge, jokes — Vera answers without tools.
- Pick a Telegram agent ONLY when the user wants to read, search, list,
  or send Telegram chats/channels/messages, or asks about content of any
  named chat/channel/person.

For Telegram tasks, ALWAYS set context.action to one of:
  "read_messages"   — read recent messages from a specific chat/channel
  "search_dialogs"  — list/find chats matching a name fragment
  "send_message"    — write to a specific chat
  "get_info"        — info about a chat (members, last activity)

Context fields by action:
  read_messages:  peer (chat name or @username), limit (default 50), days (default 1)
  search_dialogs: query (string)
  send_message:   peer, text
  get_info:       peer

Heuristics:
- "сегодня" → days=1; "вчера" → days=2; "за неделю / на неделю" → days=7;
  "за месяц" → days=30
- Extract person/chat name even if user uses diminutive or mis-spelled form
  (e.g. "евочка" → peer="евочка", "на вернаде" → peer="Veranda")
- When user mentions a brand/place name in context of "что там / анонсы /
  новости / что пишут", action=read_messages with that name as peer

Examples:
  "привет" → {"summary":"приветствие","target_agents":[],"context":{}}
  "кто ты, что умеешь" → {"summary":"знакомство","target_agents":[],"context":{}}
  "расскажи анекдот" → {"summary":"анекдот","target_agents":[],"context":{}}
  "какая столица Франции" → {"summary":"вопрос знания","target_agents":[],"context":{}}

  "прочитай последние 20 сообщений из чата Лиза" →
    {"summary":"чтение чата Лиза","target_agents":["vera-telegram"],
     "context":{"action":"read_messages","peer":"Лиза","limit":20,"days":7}}

  "о чём общались сегодня с евочкой?" →
    {"summary":"переписка с Евочкой за сегодня","target_agents":["vera-telegram"],
     "context":{"action":"read_messages","peer":"евочка","limit":50,"days":1}}

  "какие анонсы на вернаде на неделю?" →
    {"summary":"анонсы канала Veranda за неделю","target_agents":["vera-telegram"],
     "context":{"action":"read_messages","peer":"Veranda","limit":100,"days":7}}

  "найди чат про крипту" →
    {"summary":"поиск чата про крипту","target_agents":["vera-telegram"],
     "context":{"action":"search_dialogs","query":"крипта"}}

  "посмотри все чаты Veranda в названии" →
    {"summary":"список чатов Veranda","target_agents":["vera-telegram"],
     "context":{"action":"search_dialogs","query":"Veranda"}}

  "напиши Лизе что я буду через час" →
    {"summary":"отправка Лизе","target_agents":["vera-telegram"],
     "context":{"action":"send_message","peer":"Лиза","text":"буду через час"}}
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
        "Return JSON only."
    )

    try:
        from vera_shared.providers.registry import get_registry
        raw, _, _ = await get_registry().chat(
            "prefilter", [{"role": "user", "content": prompt}], system=_SYSTEM
        )
        data = json.loads(_strip_code_fence(raw))
        intent = Intent(
            summary=data.get("summary", text[:80]),
            target_agents=data.get("target_agents", []) or [],
            context=data.get("context", {}) or {},
        )
        log.info(
            "Prefilter: agents=%s ctx=%s", intent.target_agents, intent.context
        )
        return intent
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
