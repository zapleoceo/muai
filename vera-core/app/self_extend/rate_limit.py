"""Rate limiter for self-extension actions. 1 install per hour, 5 per day.
State persists in settings(key='self_extend.rate'). Per-action type."""
from datetime import datetime, timedelta

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Setting

_KEY = "self_extend.rate"
_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)
_HOUR_LIMIT = 1
_DAY_LIMIT = 5


async def _load() -> dict:
    async with get_session() as s:
        row = await s.get(Setting, _KEY)
        return dict(row.value) if (row and isinstance(row.value, dict)) else {"events": []}


async def _save(state: dict) -> None:
    async with get_session() as s:
        row = await s.get(Setting, _KEY)
        if row is None:
            s.add(Setting(key=_KEY, value=state))
        else:
            row.value = state
        await s.commit()


def _prune(events: list, now: datetime) -> list:
    cutoff = now - _DAY
    return [e for e in events if datetime.fromisoformat(e) >= cutoff]


async def check_and_consume(action: str = "install") -> tuple[bool, str]:
    """Return (allowed, reason). If allowed, the event is recorded — call
    only when actually performing the action."""
    now = datetime.utcnow()
    state = await _load()
    events = _prune(state.get("events", []), now)
    last_hour = [e for e in events if datetime.fromisoformat(e) >= now - _HOUR]
    if len(last_hour) >= _HOUR_LIMIT:
        return False, f"rate limit: {_HOUR_LIMIT}/hour reached"
    if len(events) >= _DAY_LIMIT:
        return False, f"rate limit: {_DAY_LIMIT}/day reached"
    events.append(now.isoformat())
    state["events"] = events
    state["last_action"] = action
    await _save(state)
    return True, "ok"


async def peek() -> dict:
    now = datetime.utcnow()
    state = await _load()
    events = _prune(state.get("events", []), now)
    last_hour = sum(1 for e in events if datetime.fromisoformat(e) >= now - _HOUR)
    return {
        "events_24h": len(events),
        "events_last_hour": last_hour,
        "hour_limit": _HOUR_LIMIT,
        "day_limit": _DAY_LIMIT,
        "next_allowed_at": (
            (datetime.fromisoformat(events[-1]) + _HOUR).isoformat()
            if last_hour >= _HOUR_LIMIT and events else None
        ),
    }
