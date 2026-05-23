"""Voice cloning — extract Dima's per-relationship tone from sent messages.

Two functions:
  - extract_style(relationship_id): daily scan of recent sent messages
    to this person, run through LLM with a "describe the tone" prompt,
    save as :Style node attached via [:FOR]->(:Person).
  - apply_style(draft, relationship_id): rewrite an outgoing draft to
    match the saved tone. Called by tools before they actually send.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import desc, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event
from vera_shared.llm.router import chat as llm_chat

from app.brain import identity as ID

log = logging.getLogger(__name__)


_STYLE_SYSTEM = """Ты — анализатор стиля Димы. На вход — несколько его
сообщений одному и тому же человеку. На выход — короткий профиль (2-4
строки в JSON) с полями:
  {
    "tone": "формальный|дружеский|деловой|короткий|разговорный",
    "examples": ["3-5 коротких характерных фраз"],
    "register": "ты|вы",
    "length_avg_chars": <число>
  }
Только JSON, ничего больше."""


_REWRITE_SYSTEM = """Перепиши черновик письма/сообщения так, чтобы оно
звучало как сам Дима пишет этому человеку. Учитывай стиль (тон,
обращение «ты/вы», длину, характерные фразы). Не меняй смысл, не
выдумывай факты. Ответь только переписанным текстом, без комментариев."""


async def extract_style(relationship_id: str, lookback_days: int = 30) -> dict:
    """Sample Dima's recent sent messages to relationship_id (email or
    @username), ask LLM to describe the style, save as Style node."""
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    samples = await _sample_sent(relationship_id, cutoff, limit=20)
    if not samples:
        return {"ok": False, "reason": "no sent samples"}

    joined = "\n---\n".join(s[:500] for s in samples)
    raw = await llm_chat(
        messages=[{"role": "user",
                    "content": f"Получатель: {relationship_id}\n\n{joined}"}],
        system=_STYLE_SYSTEM, capability="chat:fast",
    )
    raw = raw.strip().strip("`").lstrip("json").strip()
    try:
        import json
        profile = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("voice.extract_style: bad JSON: %s", raw[:120])
        return {"ok": False, "reason": "bad LLM JSON"}

    nid = await ID.upsert_style(
        relationship_id=relationship_id,
        tone=profile.get("tone", "?"),
        examples=profile.get("examples") or [],
    )
    return {"ok": True, "id": nid, "profile": profile}


async def apply_style(draft: str, relationship_id: str) -> str:
    """Rewrite draft to match this relationship's tone. If no Style node
    exists, return draft unchanged."""
    style = await _get_style(relationship_id)
    if style is None:
        return draft
    hint = (f"Стиль получателя: tone={style.get('tone','')}, "
            f"примеры={style.get('examples') or []}")
    out = await llm_chat(
        messages=[
            {"role": "system", "content": hint},
            {"role": "user", "content": draft},
        ],
        system=_REWRITE_SYSTEM, capability="chat:fast",
    )
    return out.strip() or draft


async def _sample_sent(relationship_id: str, cutoff: datetime,
                        limit: int) -> list[str]:
    """Pull recent sent messages to this relationship.

    For gmail we look at Event rows where metadata.from contains Dima's
    address (skip — Dima IS the sender) and the recipient matches.
    Currently we just scan content_text containing the recipient handle
    — refine when /sent indexing lands.
    """
    async with get_session() as s:
        rs = (await s.execute(
            select(Event).where(
                Event.occurred_at >= cutoff,
                Event.content_text.contains(relationship_id),
            ).order_by(desc(Event.id)).limit(limit)
        )).scalars().all()
    return [(e.content_text or "")[:1500] for e in rs]


async def _get_style(relationship_id: str) -> dict | None:
    from app.graph.client import get_graphiti
    from app.config import get_settings
    client = await get_graphiti()
    db = get_settings().neo4j_database
    async with client.driver.session(database=db) as ses:
        r = await ses.run(
            "MATCH (s:Style {relationship_id: $rid}) RETURN s "
            "ORDER BY s.updated_at DESC LIMIT 1",
            rid=relationship_id,
        )
        row = await r.single()
    if row is None:
        return None
    node = row["s"]
    return {k: node.get(k) for k in node.keys()}
