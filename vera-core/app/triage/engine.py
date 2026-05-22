import json
import logging
import re
from dataclasses import dataclass

from vera_shared.db.models import Event

log = logging.getLogger(__name__)


@dataclass
class TriageProposal:
    urgency: str          # low | medium | high | critical
    summary: str          # 1-2 sentence summary
    actions: list[dict]   # [{label, description, default?}]
    confidence: float     # 0..1
    reasoning: str
    context_used: list[str]  # what graph-evidence was used


_SYSTEM = """Ты — Vera, AI-оркестратор личных дел Димы. Живёшь в /var/www/vera.
Твоя дока — VERA.md (читается через read_text_file). Тебе пришло НОВОЕ событие
из одного из источников (Telegram, Gmail, банковский алерт, инфра-алерт, и т.д.).

Твоя задача — за один шаг:
1) Понять что это и насколько срочно
2) Использовать данный тебе КОНТЕКСТ ИЗ ГРАФА (похожие прошлые случаи,
   профили причастных сущностей, недавняя активность Димы) — это память Веры
3) Предложить 2-5 РЕАЛЬНЫХ ВАРИАНТОВ ДЕЙСТВИЯ — что Дима скорее всего захочет
   сделать. Каждый action: короткий label для кнопки (≤30 симв) + чуть больше
   description с обоснованием.
4) Если в списке TOOLS есть подходящий инструмент — добавь поля "tool"
   (имя из TOOLS) и "args" (объект параметров) к action. Тогда нажатие кнопки
   автоматически выполнит этот инструмент. НЕ ВЫДУМЫВАЙ tool, которого нет
   в TOOLS. Не клади tool в action, который требует размышления/уточнения.
5) Оценить свою уверенность 0..1.

Верни ТОЛЬКО валидный JSON, без markdown:
{
  "urgency": "low|medium|high|critical",
  "summary": "1-2 предложения по-русски, что произошло и о чём это",
  "actions": [
    {"label":"коротко","description":"что и почему","default":true,
     "tool":"имя_из_TOOLS_или_null","args":{...}},
    {"label":"...","description":"...","default":false}
  ],
  "confidence": 0.0..1.0,
  "reasoning": "1-3 предложения почему именно такие варианты",
  "context_used": ["краткий список фактов из графа которые сыграли роль"]
}

Один из actions помечается default=true — это твоя главная рекомендация.
Если событие незначительное (newsletter, авто-уведомление) — пометь low.
Если касается денег, инфры, личных отношений Димы — обычно medium/high.
Никогда не выдумывай данных которых нет в контексте."""


_RELEVANCE_TOKEN_MIN = 3   # word length floor for tokenization
_MIN_OVERLAP_RATIO = 0.10  # at least 10% of context tokens must appear in fact
_MIN_ABSOLUTE_OVERLAP = 1


def _tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    return {w for w in re.findall(r"[\w@.-]+", text)
            if len(w) >= _RELEVANCE_TOKEN_MIN}


def _is_relevant(fact: str, query_tokens: set[str],
                  entity_terms: set[str]) -> bool:
    """A retrieved episode is relevant iff it shares meaningful tokens with
    the event content OR mentions one of the event's entity identifiers
    OR is a known persona/instruction/rejection signal."""
    fact_tokens = _tokenize(fact)
    if any(term in fact.lower() for term in entity_terms):
        return True
    f_low = fact.lower()
    if any(x in f_low for x in ("игнор", "instruction", "инструкц",
                                "rejection", "дима ", "persona")):
        return True
    if not query_tokens:
        return False
    overlap = fact_tokens & query_tokens
    if len(overlap) < _MIN_ABSOLUTE_OVERLAP:
        return False
    ratio = len(overlap) / max(len(query_tokens), 1)
    return ratio >= _MIN_OVERLAP_RATIO


async def _retrieve_context(event: Event, limit: int = 8) -> list[dict]:
    """Hybrid retrieval with relevance gate.

    Graph search returns top-N by similarity, but on a sparse graph it
    will return DOMINANT episodes regardless of how unrelated they are
    (the bug where Marina's message dragged in 'domain veranda.my').
    We post-filter: keep only episodes that share tokens with the event,
    mention one of its entities, or are persona/rejection signals.
    """
    try:
        from app.graph.client import get_graphiti
        client = await get_graphiti()
        seen: dict[str, dict] = {}

        async def _add_results(results, weight: float, source_tag: str) -> None:
            for r in results:
                uuid = str(getattr(r, "uuid", "") or
                           f"{getattr(r, 'fact', '') or ''}|{source_tag}")
                fact = (getattr(r, "fact", None) or getattr(r, "name", None)
                        or str(r))
                prev = seen.get(uuid)
                score = (prev["score"] if prev else 0) + weight
                seen[uuid] = {"fact": fact, "uuid": uuid, "score": score,
                              "source": source_tag}

        content_query = (event.content_text or "")[:500]
        query_tokens = _tokenize(content_query)
        entity_terms: set[str] = set()
        for hint in (event.entity_hints or [])[:6]:
            for k in ("identifier", "name"):
                v = (hint.get(k) or "").strip().lower()
                if v and len(v) >= _RELEVANCE_TOKEN_MIN:
                    entity_terms.add(v)

        if content_query:
            await _add_results(
                await client.search(query=content_query, num_results=limit),
                1.0, "semantic",
            )

        seen_terms: set[str] = set()
        for hint in (event.entity_hints or [])[:6]:
            term = (hint.get("identifier") or hint.get("name") or "").strip()
            if not term or len(term) < _RELEVANCE_TOKEN_MIN or term.lower() in seen_terms:
                continue
            seen_terms.add(term.lower())
            try:
                await _add_results(
                    await client.search(query=term, num_results=4),
                    1.5, f"entity:{hint.get('type','?')}",
                )
            except Exception as exc:
                log.debug("entity search %r failed: %s", term, exc)

        for item in seen.values():
            f = (item["fact"] or "").lower()
            if "игнор" in f or "rejection" in f or "инструкц" in f or "instruction" in f:
                item["score"] += 2.0

        relevant = [
            x for x in seen.values()
            if _is_relevant(x["fact"], query_tokens, entity_terms)
        ]
        dropped = len(seen) - len(relevant)
        if dropped:
            log.debug("retrieval: dropped %d irrelevant of %d (event=%d)",
                      dropped, len(seen), event.id)

        ranked = sorted(relevant, key=lambda x: x["score"], reverse=True)
        return [{"fact": x["fact"], "uuid": x["uuid"]} for x in ranked[:limit]]
    except Exception as exc:
        log.warning("Graph retrieval failed: %s", exc)
        return []


def _strip_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _safe_parse(raw: str) -> dict | None:
    try:
        return json.loads(_strip_fence(raw))
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def _format_event_for_llm(event: Event, context: list[dict]) -> str:
    parts: list[str] = []
    parts.append(f"Source: {event.source}")
    parts.append(f"Category: {event.category}")
    parts.append(f"Time: {event.occurred_at.isoformat() if event.occurred_at else '?'}")
    if event.account:
        parts.append(f"Account: {event.account}")
    if event.entity_hints:
        parts.append("Entities mentioned (from adapter):")
        for h in event.entity_hints:
            parts.append(f"  - {h.get('type','?')}: {h.get('identifier') or h.get('name') or '?'}")
    parts.append("")
    parts.append("CONTENT:")
    parts.append(event.content_text or "(no text)")
    parts.append("")
    if context:
        parts.append("RELATED FROM MEMORY GRAPH (most relevant first):")
        for i, c in enumerate(context, 1):
            parts.append(f"  {i}. {c['fact']}")
    else:
        parts.append("RELATED FROM MEMORY: (nothing — first event of this kind)")
    return "\n".join(parts)


async def triage(event: Event) -> TriageProposal | None:
    context = await _retrieve_context(event)
    prompt = _format_event_for_llm(event, context)

    try:
        from app.persona import digest as persona_digest
        persona = await persona_digest.current()
        if persona and persona.get("bullets"):
            prompt += "\n\nPERSONA (Дима, наблюдаемые шаблоны):\n" + \
                      "\n".join(f"- {b}" for b in persona["bullets"][:15])
    except Exception as exc:
        log.warning("persona read failed: %s", exc)

    try:
        from app.orchestrator.tool_router import collect_tools, format_tools_for_prompt
        specs, _ = await collect_tools()
        tools_block = "\n\nTOOLS:\n" + format_tools_for_prompt(specs)
    except Exception as exc:
        log.warning("collect_tools for triage failed: %s", exc)
        tools_block = "\n\nTOOLS: (нет доступных инструментов)"

    try:
        from vera_shared.llm import chat
        raw = await chat(
            [{"role": "user", "content": prompt + tools_block}],
            system=_SYSTEM,
            capability="chat:fast",
        )
    except Exception as exc:
        log.warning("Triage LLM failed: %s", exc)
        return None

    data = _safe_parse(raw)
    if not data:
        log.warning("Triage LLM returned non-JSON: %r", raw[:200])
        return None

    actions = data.get("actions") or []
    if not isinstance(actions, list) or not actions:
        return None

    # normalise actions
    normalised: list[dict] = []
    for a in actions[:5]:
        if not isinstance(a, dict):
            continue
        label = str(a.get("label", "")).strip()[:30]
        if not label:
            continue
        item = {
            "label": label,
            "description": str(a.get("description", "")).strip()[:300],
            "default": bool(a.get("default", False)),
        }
        tool = a.get("tool")
        if isinstance(tool, str) and tool.strip():
            item["tool"] = tool.strip()
            args = a.get("args")
            item["args"] = args if isinstance(args, dict) else {}
        normalised.append(item)
    if not normalised:
        return None

    # «Как в прошлый раз»: pull recent decisions for this sender from the
    # replay table, prepend the most-frequent one as default action. This
    # is the FAST PATH for "do what we did last time" — no LLM re-derivation.
    try:
        from app.triage.replay import suggest as suggest_replays
        prior = await suggest_replays(event, limit=3)
    except Exception as exc:
        log.debug("replay suggest failed: %s", exc)
        prior = []
    confidence = float(data.get("confidence", 0.5) or 0.5)
    if prior:
        top = prior[0]
        count = int(top.get("count", 1) or 1)
        replay_action = {
            "label": f"⭐ Как в прошлый раз: {top['label'][:24]}",
            "description": (
                f"Повторено {count} раз для этого отправителя. "
                f"Последний раз: {top.get('last_used_at', '')[:10]}."
            ),
            "default": True,
            "replay": True,
            "replay_count": count,
        }
        if top.get("tool"):
            replay_action["tool"] = top["tool"]
            replay_action["args"] = top.get("args") or {}
        # Dedup: drop any LLM-suggested action that is functionally the
        # same as the replay action (same tool + same args).
        replay_sig = (replay_action.get("tool"),
                      tuple(sorted((replay_action.get("args") or {}).items())))
        normalised = [
            a for a in normalised
            if (a.get("tool"), tuple(sorted((a.get("args") or {}).items()))) != replay_sig
        ]
        for a in normalised:
            a["default"] = False
        normalised = [replay_action] + normalised
        # Confidence derived from repeat count, OVERRIDING the LLM's
        # gut-feel guess. Curve: 1 - 0.5/count. So:
        #   1 → 0.50    3 → 0.83    5 → 0.90
        #   10 → 0.95   20 → 0.975  50 → 0.99
        # Single threshold gates everything — user picks how cautious
        # via preferences.auto_threshold (default 0.95 = ≥10 repeats).
        confidence = round(1 - 0.5 / count, 3)
    elif not any(a["default"] for a in normalised):
        normalised[0]["default"] = True

    return TriageProposal(
        urgency=str(data.get("urgency", "medium")).lower(),
        summary=str(data.get("summary", ""))[:500],
        actions=normalised,
        confidence=round(min(confidence, 1.0), 3),
        reasoning=str(data.get("reasoning", ""))[:500],
        context_used=[str(x) for x in (data.get("context_used") or [])][:10],
    )
