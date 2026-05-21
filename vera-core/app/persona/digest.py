"""Persona distillation: LLM reads recent decisions, emits compact preference notes."""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event, Setting

log = logging.getLogger(__name__)

_SYSTEM = """Ты — Vera. По решениям Димы за последние дни составь компактный
персона-конспект: предпочтения, шаблоны реакций, что игнорирует, что
обычно делает сам. Только факты, наблюдаемые из решений. Никаких
выдумок. 5-15 буллетов. Русский.

Верни ТОЛЬКО JSON:
{
  "bullets": ["...", "..."],
  "covers_events": <int>,
  "generated_at": "<iso>"
}"""


async def _collect_decisions(days: int = 14, limit: int = 200) -> list[dict]:
    since = datetime.utcnow() - timedelta(days=days)
    async with get_session() as session:
        result = await session.execute(
            select(Event)
            .where(Event.created_at >= since)
            .where(Event.triage_status.in_(["decided", "executed", "auto_executed"]))
            .order_by(Event.created_at.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
    out: list[dict] = []
    for r in rows:
        tr = r.triage_result or {}
        choice = tr.get("user_choice") or {}
        out.append({
            "source": r.source,
            "category": r.category,
            "summary": (tr.get("summary") or "")[:200],
            "chose": choice.get("label"),
            "auto": bool(choice.get("auto")),
        })
    return out


async def regenerate() -> dict:
    decisions = await _collect_decisions()
    if not decisions:
        return {"ok": False, "reason": "no decisions yet"}
    prompt = json.dumps({"decisions": decisions}, ensure_ascii=False)
    from vera_shared.llm import chat
    raw = await chat(
        [{"role": "user", "content": prompt}],
        system=_SYSTEM, capability="chat:smart",
    )
    try:
        data = json.loads(raw.strip().lstrip("`").rstrip("`"))
    except Exception:
        log.warning("persona LLM returned non-JSON: %r", raw[:200])
        return {"ok": False, "reason": "non-json"}
    data["covers_events"] = len(decisions)
    data["generated_at"] = datetime.utcnow().isoformat()

    async with get_session() as session:
        row = await session.get(Setting, "persona")
        if row is None:
            row = Setting(key="persona", value=data)
            session.add(row)
        else:
            row.value = data
        await session.commit()
    return {"ok": True, "covers_events": len(decisions), "bullets": data.get("bullets", [])}


async def current() -> dict | None:
    async with get_session() as session:
        row = await session.get(Setting, "persona")
        return row.value if row else None
