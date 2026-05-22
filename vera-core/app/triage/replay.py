"""Decision replay — fast lookup of past user decisions keyed by sender,
used to surface 'do what you did last time' as a one-tap option."""
from datetime import datetime, timedelta

from sqlalchemy import desc, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import DecisionReplay, Event

_RECENT = timedelta(days=60)


import re as _re

_EMAIL_RE = _re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_USERNAME_RE = _re.compile(r"@(\w+)")


def _normalize_sender(raw: str) -> str:
    """Reduce a sender string to a stable key. Strategies:
      - email present anywhere → lowercase email
      - @username → lowercase username
      - else → lowercased, stripped, non-empty
    Examples:
      '"Joinposter.com" <contact@joinposter.com>' → 'contact@joinposter.com'
      'VerandaBot (@VerandamyBot)' → 'verandamybot'
      'Eva' → 'eva'
    """
    raw = (raw or "").strip()
    m = _EMAIL_RE.search(raw)
    if m:
        return m.group(0).lower()
    m = _USERNAME_RE.search(raw)
    if m:
        return m.group(1).lower()
    return raw.lower() if raw else ""


def _sender_key(event: Event) -> str | None:
    """Stable key identifying the 'who' behind an event — same key across
    rows even when sender display name changes."""
    for hint in (event.entity_hints or []):
        if hint.get("type") == "person":
            for field in ("identifier", "name"):
                v = hint.get(field)
                if v:
                    norm = _normalize_sender(str(v))
                    if norm:
                        return norm
    return _normalize_sender(event.account or "") or None


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


async def reset(event: Event, reason: str = "") -> None:
    """Drop all replay rows for this sender so confidence falls back to
    the LLM's baseline. Called after Dima undoes an auto-execution."""
    sender = _sender_key(event)
    if not sender:
        return
    from sqlalchemy import delete
    async with get_session() as s:
        await s.execute(
            delete(DecisionReplay)
            .where(DecisionReplay.source == event.source)
            .where(DecisionReplay.sender_key == sender)
        )
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
