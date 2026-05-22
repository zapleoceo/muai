"""Decision replay — fast lookup of past user decisions keyed by sender,
used to surface 'do what you did last time' as a one-tap option."""
from datetime import datetime, timedelta

from sqlalchemy import desc, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import DecisionReplay, Event

_RECENT = timedelta(days=60)


def _sender_key(event: Event) -> str | None:
    """Stable key identifying the 'who' behind an event — across rows."""
    for hint in (event.entity_hints or []):
        if hint.get("type") == "person":
            ident = hint.get("identifier") or hint.get("name")
            if ident:
                return str(ident).lower()
    # Fall back to account (e.g. gmail address) when there's no sender hint
    return (event.account or "").lower() or None


async def record(event: Event, label: str, tool: str | None,
                  args: dict | None) -> None:
    sender = _sender_key(event)
    if not sender:
        return
    async with get_session() as s:
        result = await s.execute(
            select(DecisionReplay)
            .where(DecisionReplay.source == event.source)
            .where(DecisionReplay.sender_key == sender)
            .where(DecisionReplay.tool == (tool or None))
            .order_by(desc(DecisionReplay.id))
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            row.count = (row.count or 0) + 1
            row.last_used_at = datetime.utcnow()
            row.label = label  # latest wording wins
            row.args = args
            await s.commit()
            return
        s.add(DecisionReplay(
            source=event.source, sender_key=sender,
            label=label[:200], tool=tool, args=args or {},
            count=1, last_used_at=datetime.utcnow(),
        ))
        await s.commit()


async def suggest(event: Event, limit: int = 3) -> list[dict]:
    """Return prior decisions for this sender, most-frequent first."""
    sender = _sender_key(event)
    if not sender:
        return []
    cutoff = datetime.utcnow() - _RECENT
    async with get_session() as s:
        result = await s.execute(
            select(DecisionReplay)
            .where(DecisionReplay.source == event.source)
            .where(DecisionReplay.sender_key == sender)
            .where(DecisionReplay.last_used_at >= cutoff)
            .order_by(desc(DecisionReplay.count), desc(DecisionReplay.last_used_at))
            .limit(limit)
        )
        return [
            {"label": r.label, "tool": r.tool, "args": r.args or {},
             "count": r.count, "last_used_at": r.last_used_at.isoformat()}
            for r in result.scalars().all()
        ]
