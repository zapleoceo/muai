"""Cross-name identity resolution — «Маша» и «Matia Ivanova» это один человек?

Three layers, all owner-approved (nothing merges automatically):

1. Name canonicalisation (`identity_names`) — diminutives (Маша→мария) +
   RU↔LAT transliteration fold, so «Оля», «Ольга Олеговая» и «Olga» land in
   one bucket.
2. Candidate pairs (`identity_candidates`) — same canonical first name AND a
   corroborating signal: shared chat membership, same canonical last name, or
   different sources (gmail/instagram entity vs telegram entity — the
   cross-channel case).
3. LLM judge (here) — Vera reads both dossiers and answers same/different/unsure
   with a reason; verdicts land in `merge_suggestions` (migration 016, SQL in
   `identity_store`) for the owner to accept (→ merge_entities) or reject on
   the dashboard.

Person-vs-organization for e-mail senders lives in `identity_kind`. Every
moved name is re-exported here so existing imports keep working.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from vera_shared.graph.dedup import get_entity_dossiers
from vera_shared.graph.identity_candidates import find_identity_candidates
from vera_shared.graph.identity_kind import entity_kind_for_email
from vera_shared.graph.identity_names import canonical_name_parts
from vera_shared.graph.identity_store import (
    list_pending_suggestions,
    save_suggestion,
    set_suggestion_status,
)

__all__ = [
    "IDENTITY_JSON_SCHEMA",
    "canonical_name_parts",
    "entity_kind_for_email",
    "find_identity_candidates",
    "judge_pair",
    "list_pending_suggestions",
    "run_identity_analysis",
    "save_suggestion",
    "set_suggestion_status",
]

log = logging.getLogger(__name__)

# ─── LLM judge ──────────────────────────────────────────────────────────────


IDENTITY_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "identity_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string",
                            "enum": ["same", "different", "unsure"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
            "required": ["verdict", "confidence", "reason"],
            "additionalProperties": False,
        },
    },
}

_JUDGE_PROMPT = """Ты — Вера, память Димы. Реши, один ли это человек.

Сущность A (#{a_id}):
{a_block}

Сущность B (#{b_id}):
{b_block}

Сигнал-кандидат: {signal}.

Учитывай: уменьшительные и транслит имён (Маша=Мария=Masha), стиль сообщений,
пересечение чатов/тем, проект. РАЗНЫЕ tg_id сами по себе НЕ значат разных
людей (второй аккаунт, почта vs телеграм), но требуют более сильных улик.
Верни СТРОГО JSON: verdict (same|different|unsure), confidence 0..1,
reason — одна фраза на русском с главной уликой."""


def _dossier_block(d: dict) -> str:
    chats = ", ".join(f"{c} ({n})" for c, n in d.get("top_chats", [])) or "—"
    samples = " | ".join(d.get("samples", [])[:2]) or "—"
    return (f"имя: {d.get('name')}; username: @{d.get('username') or '—'}; "
            f"проект: {d.get('dom_project') or '—'}; сообщений: {d.get('msg_count', 0)}\n"
            f"чаты: {chats}\nпримеры: {samples}")


async def judge_pair(a_id: int, b_id: int, signal: str) -> dict[str, Any]:
    """One LLM call → verdict dict. Raises on broker failure (caller decides).
    chat_async (submit+poll /v1/jobs) — брокер удалил синхронный /v1/chat."""
    from vera_shared.llm.client import chat_async
    dossiers = await get_entity_dossiers([a_id, b_id])
    prompt = _JUDGE_PROMPT.format(
        a_id=a_id, a_block=_dossier_block(dossiers[a_id]),
        b_id=b_id, b_block=_dossier_block(dossiers[b_id]),
        signal=signal,
    )
    answer, _meta = await chat_async(
        messages=[{"role": "user", "content": prompt}],
        capability="chat:smart", max_tokens=300, temperature=0.2,
        workflow="identity", response_format=IDENTITY_JSON_SCHEMA,
    )
    parsed = json.loads(answer.strip().strip("`").removeprefix("json"))
    verdict = parsed.get("verdict")
    if verdict not in {"same", "different", "unsure"}:
        raise ValueError(f"bad verdict: {verdict!r}")
    return {"verdict": verdict,
            "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0)))),
            "reason": str(parsed.get("reason", ""))[:500]}


async def run_identity_analysis(max_pairs: int = 12) -> dict[str, int]:
    """One analysis round: candidates → judge → store. Per-pair failures are
    skipped (broker hiccups shouldn't kill the round)."""
    pairs = await find_identity_candidates(limit=max_pairs)
    stats = {"judged": 0, "same": 0, "unsure": 0, "different": 0, "failed": 0}
    for a, b, signal in pairs:
        try:
            verdict = await judge_pair(a, b, signal)
        except Exception as e:
            log.warning("identity judge %s/%s failed: %s", a, b, e)
            stats["failed"] += 1
            continue
        await save_suggestion(a, b, verdict)
        stats["judged"] += 1
        stats[verdict["verdict"]] += 1
    log.info("identity analysis: %s", stats)
    return stats
