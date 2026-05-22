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
        # Compact result preview for the one-liner card.
        res = (auto_exec.get("result") or {})
        preview = ""
        if isinstance(res, dict):
            preview = str(res.get("result") or res.get("error") or "")
        else:
            preview = str(res)
        card_msg_id = await send_card(
            event_id, e.source, e.category, proposal,
            auto_exec={"label": auto_action["label"], "ok": auto_exec.get("ok"),
                       "result_preview": preview},
        )
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
    """Brain-driven auto-execute: no Trigger rows required.

    Fires when ALL of:
      1. The default action has a tool AND the tool is auto-safe (read-only
         or idempotent label op — never send/post)
      2. proposal.confidence >= preferences.auto_threshold (default 0.95)
      3. The action is a replay of a prior decision Dima made for this
         sender ≥ preferences.auto_min_repeats times (default 3) — so we
         only auto-fire on patterns Dima visibly approved
    """
    default = next((a for a in proposal.actions if a.get("default") and a.get("tool")), None)
    if default is None:
        return None
    from app.orchestrator.tool_router import is_auto_safe
    if not is_auto_safe(default["tool"]):
        return None
    from app.bot import preferences
    prefs = await preferences.get_all()
    threshold = float(prefs.get("auto_threshold", 0.95))
    min_repeats = int(prefs.get("auto_min_repeats", 3))
    if proposal.confidence < threshold:
        return None
    # Replay action carries `replay: true` flag. Count is in description.
    if not default.get("replay"):
        return None
    from app.triage import replay as rp
    prior = await rp.suggest(event, limit=1)
    if not prior or prior[0].get("count", 0) < min_repeats:
        return None
    return default


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
    """Legacy helper kept for backward compat; new card path uses
    send_card(..., auto_exec={...}) instead."""
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


async def record_undo(event_id: int) -> str:
    """Undo an auto-executed decision: mark status, write strong rejection
    episode + decrement the replay count so confidence drops below the
    auto threshold next time. Tool-level undo is best-effort and depends
    on the tool (some are reversible, some not — we only invalidate the
    learning signal, leaving Dima to manually undo side-effects if any)."""
    async with get_session() as session:
        event = await session.get(Event, event_id)
        if event is None:
            return "событие не найдено"
        sender = _sender_of(event)
        merged = dict(event.triage_result or {})
        merged["undone"] = True
        event.triage_result = merged
        event.triage_status = "undone"
        await session.commit()

    from app.graph import write as gw
    from app.triage import replay as rp
    summary = merged.get("summary") or (event.content_text or "")[:200]
    gw.write_rejection(event_id, event.source, sender,
                       f"АВТО-ДЕЙСТВИЕ ОТКАЧЕНО. {summary}")
    # Reset replay count for this sender so we don't auto-fire again
    # until Dima rebuilds trust manually.
    try:
        await rp.reset(event, reason="auto-action undone")
    except Exception as exc:
        log.warning("replay reset failed: %s", exc)
    return f"замена счётчика повторов для {sender or 'отправителя'}"


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
