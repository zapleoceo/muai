import asyncio
import logging

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event

from app.triage.card import send_card
from app.triage.engine import triage

log = logging.getLogger(__name__)


async def _run_triage(event_id: int) -> None:
    async with get_session() as session:
        event = await session.get(Event, event_id)
        if event is None:
            return
        # detach for use outside session
        e = event

    proposal = await triage(e)
    if proposal is None:
        async with get_session() as session:
            row = await session.get(Event, event_id)
            if row:
                row.triage_status = "failed"
                await session.commit()
        return

    card_msg_id = await send_card(event_id, e.source, e.category, proposal)

    async with get_session() as session:
        row = await session.get(Event, event_id)
        if row:
            row.triage_status = "awaiting_user" if card_msg_id else "proposal_only"
            row.triage_result = {
                "urgency": proposal.urgency,
                "summary": proposal.summary,
                "actions": proposal.actions,
                "confidence": proposal.confidence,
                "reasoning": proposal.reasoning,
                "context_used": proposal.context_used,
                "card_message_id": card_msg_id,
            }
            await session.commit()
    log.info("Triage done for event %d: %s (msg=%s)", event_id, proposal.urgency, card_msg_id)


def schedule_triage(event_id: int) -> None:
    asyncio.create_task(_run_triage(event_id))


async def record_user_decision(event_id: int, choice: str) -> dict | None:
    """choice = action_index | 'custom' | 'ignore'. Returns the chosen action dict."""
    async with get_session() as session:
        event = await session.get(Event, event_id)
        if not event or not event.triage_result:
            return None
        actions = event.triage_result.get("actions") or []
        chosen: dict | None = None
        if choice.isdigit():
            idx = int(choice)
            if 0 <= idx < len(actions):
                chosen = dict(actions[idx])
        elif choice == "custom":
            chosen = {"label": "Свой ответ", "description": "Пользователь ответит сам"}
        elif choice == "ignore":
            chosen = {"label": "Игнорировать", "description": "Дима пропускает"}

        if chosen is None:
            return None

        result = dict(event.triage_result)
        result["user_choice"] = chosen
        event.triage_result = result
        event.triage_status = "decided"
        await session.commit()
        return chosen
