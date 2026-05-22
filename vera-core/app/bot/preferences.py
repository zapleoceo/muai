"""User preferences stored in settings table.
Single source of truth so handler, callbacks, and the LLM-callable tool
read the same thing."""
from vera_shared.db.engine import get_session
from vera_shared.db.models import Setting

_KEY = "user_prefs"

_DEFAULTS: dict = {
    "delete_card_after_decision": False,   # true → bot.delete_message after action
    "execution_recap_in_dm": False,        # send tool result as separate DM message
    "auto_threshold": 0.95,                # confidence ≥ X → auto-execute.
                                           # confidence = 1 - 0.5/count, so
                                           # 0.85 → 4 repeats, 0.90 → 5,
                                           # 0.95 → 10, 0.99 → 50.
    "use_topics": False,                   # post each event into its own forum topic
    "forum_chat_id": 0,                    # supergroup with forums enabled (negative int)
    "close_topic_on_decision": True,       # close forum topic once Dima decided
    "delete_topic_on_decision": True,      # delete (not just close) topic after decision
}


async def get_all() -> dict:
    async with get_session() as s:
        row = await s.get(Setting, _KEY)
    base = dict(_DEFAULTS)
    if row and isinstance(row.value, dict):
        base.update(row.value)
    return base


async def get(key: str):
    prefs = await get_all()
    return prefs.get(key, _DEFAULTS.get(key))


async def set(key: str, value):
    if key not in _DEFAULTS:
        raise ValueError(f"unknown preference {key!r} (allowed: {list(_DEFAULTS)})")
    async with get_session() as s:
        row = await s.get(Setting, _KEY)
        data = dict(row.value) if (row and isinstance(row.value, dict)) else {}
        data[key] = value
        if row is None:
            s.add(Setting(key=_KEY, value=data))
        else:
            row.value = data
        await s.commit()
    return value


def known_keys() -> list[str]:
    return list(_DEFAULTS.keys())
