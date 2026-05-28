import asyncio
import logging

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Event

from app.triage.card import send_card
from app.triage.engine import triage

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

    # Silence mode: skip card if v3 brain scores this event below threshold.
    try:
        from app.brain.observability import get_card_threshold
        min_score = await get_card_threshold()
    except Exception:
        min_score = 5.0
    try:
        from app.decide.dispatch import decide as v3_decide
        v3 = await v3_decide(e.entity_hints or [])
        v3_score = v3.chosen.score if v3.chosen else 0.0
    except Exception as exc:
        log.debug("v3 score lookup failed for event %s: %s", event_id, exc)
        v3_score = 999.0  # show card if v3 is broken
    if v3_score < min_score:
        log.info("Event %d silenced (v3=%.2f < %.2f)", event_id, v3_score, min_score)
        async with get_session() as session:
            row = await session.get(Event, event_id)
            if row:
                row.triage_status = "silenced"
                row.triage_result = {
                    "silenced": True, "v3_score": v3_score,
                    "summary": proposal.summary,
                    "urgency": proposal.urgency,
                    "actions": proposal.actions,
                }
                await session.commit()
        return

    auto_action = await _pick_auto_action(e, proposal)
    auto_exec: dict | None = None

    if auto_action is not None:
        auto_exec = await _execute_auto(event_id, auto_action)
        res = (auto_exec.get("result") or {})
        preview = str(res.get("result") or res.get("error") or "") if isinstance(res, dict) else str(res)
        card = await send_card(
            event_id, e.source, e.category, proposal,
            auto_exec={"label": auto_action["label"], "ok": auto_exec.get("ok"),
                       "result_preview": preview},
        )
    else:
        card = await send_card(event_id, e.source, e.category, proposal)

    card_msg_id    = card.get("msg_id")    if card else None
    card_thread_id = card.get("thread_id") if card else None
    card_chat_id   = card.get("chat_id")   if card else None

    async with get_session() as session:
        row = await session.get(Event, event_id)
        if row:
            if auto_exec is not None:
                row.triage_status = "auto_executed" if auto_exec.get("ok") else "auto_failed"
            else:
                row.triage_status = "awaiting_user" if card_msg_id else "proposal_only"
            payload = {
                "urgency":      proposal.urgency,
                "summary":      proposal.summary,
                "actions":      proposal.actions,
                "confidence":   proposal.confidence,
                "reasoning":    proposal.reasoning,
                "context_used": proposal.context_used,
                "card_message_id": card_msg_id,
                "card_thread_id":  card_thread_id,
                "card_chat_id":    card_chat_id,
            }
            if auto_exec is not None:
                payload["executions"]  = [auto_exec]
                payload["user_choice"] = {"label": auto_action["label"], "auto": True}
            row.triage_result = payload
            await session.commit()
    log.info("Triage done for event %d: %s (msg=%s, auto=%s)",
             event_id, proposal.urgency, card_msg_id, auto_exec is not None)


async def _pick_auto_action(event: Event, proposal) -> dict | None:
    """Brain-driven auto-execute.

    Fires when ALL of:
      1. The default action has a tool AND tool is in AUTO_SAFE_TOOLS
      2. The action came from replay history (not LLM gut-feel)
      3. proposal.confidence >= preferences.auto_threshold

    Confidence is derived from replay count via `1 - 0.5/count`, so a
    single threshold (the only knob) maps directly to a repeat-count
    requirement:
      threshold 0.85 → 4 repeats   threshold 0.95 → 10 repeats
      threshold 0.90 → 5 repeats   threshold 0.99 → 50 repeats
    """
    default = next((a for a in proposal.actions if a.get("default") and a.get("tool")), None)
    if default is None or not default.get("replay"):
        return None
    from app.orchestrator.tool_router import is_auto_safe
    if not is_auto_safe(default["tool"]):
        return None
    from app.bot import preferences
    threshold = float((await preferences.get_all()).get("auto_threshold", 0.95))
    if proposal.confidence < threshold:
        return None
    return default


def _event_payload(event: Event) -> dict:
    return {
        "source":       event.source,
        "category":     event.category,
        "account":      event.account,
        "content_text": event.content_text or "",
        "entity_hints": event.entity_hints or [],
    }


async def _execute_auto(event_id: int, action: dict) -> dict:
    from app.orchestrator.tool_router import call_tool, collect_tools, is_auto_safe
    tool = action["tool"]
    if not is_auto_safe(tool):
        log.warning("auto-skip: tool %r not in AUTO_SAFE_TOOLS for event %d", tool, event_id)
        return {"tool": tool, "args": action.get("args") or {},
                "result": {"ok": False, "error": f"tool '{tool}' not auto-safe"},
                "ok": False, "auto": True, "skipped": True}
    _, route = await collect_tools()
    result = await call_tool(route, tool, action.get("args") or {})
    return {"tool": tool, "args": action.get("args") or {},
            "result": result, "ok": bool(result.get("ok")), "auto": True}


def schedule_triage(event_id: int) -> None:
    from app.common.bg import spawn
    spawn(_run_triage(event_id), name=f"triage-{event_id}")


async def record_user_decision(event_id: int, choice: str) -> dict | None:
    """Record Dima's explicit choice for an event.

    Three writes, always in this order:
      1. DB status update (synchronous, blocking — must succeed)
      2. Graphiti episode + DecisionReplay (learning signal — always runs)
      3. Pattern node bump in Neo4j (v3 brain — best-effort, won't break 1+2)
    """
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

    # --- 2. Learning signal: Graphiti + DecisionReplay ----------------------
    # Must run unconditionally — this is the feedback loop that makes Vera
    # smarter. Not inside any try/except that could accidentally swallow it.
    sender  = _sender_of(event)
    summary = result.get("summary") or (event.content_text or "")[:200]

    from app.graph import write as gw
    from app.triage import replay
    if choice == "ignore":
        gw.write_rejection(event_id, event.source, sender, summary)
        await replay.record(event, "Игнорировать", None, None)
    else:
        label = chosen.get("label", "?")
        gw.write_decision(event_id, event.source, sender,
                          label, chosen.get("tool"), summary)
        await replay.record(event, label, chosen.get("tool"), chosen.get("args"))

    # --- 3. v3 Pattern node (best-effort, isolated) -------------------------
    try:
        from app.brain import patterns as P
        hints = event.entity_hints or []
        ctx   = P.context_key_for(hints)
        sig   = P.signature_for(hints, chosen.get("label", ""))
        await P.upsert_confirmation(sig, ctx,
                                     action_label=chosen.get("label", ""),
                                     tool=chosen.get("tool"),
                                     args=chosen.get("args"))
    except Exception as exc:
        log.debug("Pattern confirm failed for event %d: %s", event_id, exc)

    return chosen


def _sender_of(event: Event) -> str | None:
    for hint in (event.entity_hints or []):
        if hint.get("type") == "person":
            return hint.get("identifier") or hint.get("name")
    return None


async def record_undo(event_id: int) -> str:
    """Undo an auto-executed decision: mark status, write strong rejection
    episode + decrement the replay count so confidence drops below the
    auto threshold next time."""
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
    try:
        await rp.reset(event, reason="auto-action undone")
    except Exception as exc:
        log.warning("replay reset failed: %s", exc)

    # v3: correct the Pattern so its weight drops
    try:
        from app.brain import patterns as P
        chosen = (merged.get("user_choice") or {})
        hints  = event.entity_hints or []
        ctx    = P.context_key_for(hints)
        sig    = P.signature_for(hints, chosen.get("label", ""))
        await P.upsert_correction(sig, ctx,
                                   action_label=chosen.get("label", ""),
                                   tool=chosen.get("tool"),
                                   args=chosen.get("args"))
    except Exception as exc:
        log.debug("Pattern correction failed for event %d: %s", event_id, exc)

    return f"счётчик сброшен для {sender or 'отправителя'}"


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
