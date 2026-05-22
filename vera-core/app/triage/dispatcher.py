import asyncio
import logging

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event, Trigger

from app.triage.card import send_card
from app.triage.engine import triage
from app.triggers.predicates import matches

log = logging.getLogger(__name__)


_FINAL_STATUSES = frozenset({
    "decided", "executed", "execute_failed",
    "auto_executed", "auto_failed", "failed", "proposal_only",
    "awaiting_user",  # already shown a card; user hasn't responded
    "expired",        # cleaned up by /api/admin/expire-stale-events
})


async def _run_triage(event_id: int) -> None:
    async with get_session() as session:
        event = await session.get(Event, event_id)
        if event is None:
            return
        # GUARD: one trigger → one triage. If this event has already been
        # processed (any non-pending status), refuse to re-do it. Decisions
        # live in the brain via graph episodes (write_decision / write_
        # rejection) — that's where future similar events should pull from,
        # not by re-running triage on the same row.
        if event.triage_status in _FINAL_STATUSES:
            log.info("Triage skip: event %d already in status %s",
                     event_id, event.triage_status)
            return
        e = event

    proposal = await triage(e)
    if proposal is None:
        async with get_session() as session:
            row = await session.get(Event, event_id)
            if row:
                row.triage_status = "failed"
                await session.commit()
        return

    auto_action = await _pick_auto_action(e, proposal)
    card_msg_id = None
    auto_exec: dict | None = None

    if auto_action is not None:
        auto_exec = await _execute_auto(event_id, auto_action)
        await send_card(event_id, e.source, e.category, proposal,
                        auto_note=_format_auto_note(auto_action, auto_exec))
    else:
        card_msg_id = await send_card(event_id, e.source, e.category, proposal)

    async with get_session() as session:
        row = await session.get(Event, event_id)
        if row:
            if auto_exec is not None:
                row.triage_status = "auto_executed" if auto_exec.get("ok") else "auto_failed"
            else:
                row.triage_status = "awaiting_user" if card_msg_id else "proposal_only"
            payload = {
                "urgency": proposal.urgency,
                "summary": proposal.summary,
                "actions": proposal.actions,
                "confidence": proposal.confidence,
                "reasoning": proposal.reasoning,
                "context_used": proposal.context_used,
                "card_message_id": card_msg_id,
            }
            if auto_exec is not None:
                payload["executions"] = [auto_exec]
                payload["user_choice"] = {"label": auto_action["label"], "auto": True}
            row.triage_result = payload
            await session.commit()
    log.info("Triage done for event %d: %s (msg=%s, auto=%s)",
             event_id, proposal.urgency, card_msg_id, auto_exec is not None)


async def _pick_auto_action(event: Event, proposal) -> dict | None:
    """Return the default action iff a matching trigger says auto-execute."""
    default = next((a for a in proposal.actions if a.get("default") and a.get("tool")), None)
    if default is None:
        return None
    async with get_session() as session:
        result = await session.execute(
            select(Trigger).where(
                Trigger.source == event.source,
                Trigger.enabled == True,
                Trigger.auto_confidence > 0,
            )
        )
        triggers = result.scalars().all()
    for t in triggers:
        if t.account and event.account and t.account != event.account:
            continue
        if t.predicate and not matches(t.predicate, _event_payload(event)):
            continue
        if proposal.confidence >= t.auto_confidence:
            return default
    return None


def _event_payload(event: Event) -> dict:
    return {
        "source": event.source,
        "category": event.category,
        "account": event.account,
        "content_text": event.content_text or "",
        "entity_hints": event.entity_hints or [],
    }


async def _execute_auto(event_id: int, action: dict) -> dict:
    from app.orchestrator.tool_router import call_tool, collect_tools, is_auto_safe
    tool = action["tool"]
    if not is_auto_safe(tool):
        log.warning("auto-skip: tool %r not in AUTO_SAFE_TOOLS for event %d",
                    tool, event_id)
        return {"tool": tool, "args": action.get("args") or {},
                "result": {"ok": False, "error": f"tool '{tool}' not auto-safe"},
                "ok": False, "auto": True, "skipped": True}
    _, route = await collect_tools()
    result = await call_tool(route, tool, action.get("args") or {})
    return {"tool": tool, "args": action.get("args") or {}, "result": result,
            "ok": bool(result.get("ok")), "auto": True}


def _format_auto_note(action: dict, exec_result: dict) -> str:
    mark = "✅" if exec_result.get("ok") else "⚠️"
    return f"{mark} <b>Auto:</b> {action['label']}"


def schedule_triage(event_id: int) -> None:
    from app.common.bg import spawn
    spawn(_run_triage(event_id), name=f"triage-{event_id}")


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

        # Persist decision to Graphiti so future similar events surface it.
        from app.graph import write as gw
        from app.triage import replay
        sender = _sender_of(event)
        summary = result.get("summary") or (event.content_text or "")[:200]
        if choice == "ignore":
            gw.write_rejection(event_id, event.source, sender, summary)
            await replay.record(event, "Игнорировать", None, None)
        else:
            label = chosen.get("label", "?")
            gw.write_decision(event_id, event.source, sender,
                              label, chosen.get("tool"), summary)
            await replay.record(event, label, chosen.get("tool"),
                                 chosen.get("args"))
        return chosen


def _sender_of(event: Event) -> str | None:
    for hint in (event.entity_hints or []):
        if hint.get("type") == "person":
            return hint.get("identifier") or hint.get("name")
    return None


async def save_execution(event_id: int, tool: str, args: dict, result: dict) -> None:
    async with get_session() as session:
        event = await session.get(Event, event_id)
        if not event or not event.triage_result:
            return
        merged = dict(event.triage_result)
        execs = list(merged.get("executions") or [])
        execs.append({"tool": tool, "args": args, "result": result})
        merged["executions"] = execs
        event.triage_result = merged
        event.triage_status = "executed" if result.get("ok") else "execute_failed"
        await session.commit()

        from app.graph import write as gw
        gw.write_execution(event_id, tool, bool(result.get("ok")), args,
                           _sender_of(event))
