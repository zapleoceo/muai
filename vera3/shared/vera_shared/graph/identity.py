"""Cross-name identity resolution — «Маша» и «Matia Ivanova» это один человек?

Three layers, all owner-approved (nothing merges automatically):

1. Name canonicalisation — diminutives (Маша→мария) + RU↔LAT transliteration
   fold, so «Оля», «Ольга Олеговая» и «Olga» land in one bucket.
2. Candidate pairs — same canonical first name AND a corroborating signal:
   shared chat membership, same canonical last name, or different sources
   (gmail/instagram entity vs telegram entity — the cross-channel case).
3. LLM judge — Vera reads both dossiers and answers same/different/unsure
   with a reason; verdicts land in `merge_suggestions` (migration 016) for
   the owner to accept (→ merge_entities) or reject on the dashboard.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import text

from vera_shared.db.engine import get_session
from vera_shared.graph.dedup import _as_dict, get_entity_dossiers

log = logging.getLogger(__name__)

# Уменьшительное/полное → каноническая форма. Оба направления входят в словарь,
# чтобы «Маша», «Мария» и «Masha» сошлись в 'мария'.
_DIMINUTIVES: dict[str, str] = {
    "маша": "мария", "мари": "мария", "мария": "мария", "маруся": "мария",
    # транслит-написания, которые фолдятся не в словарную форму
    "маря": "мария", "мариа": "мария", "мариия": "мария",
    "олга": "ольга", "олха": "ольга", "олья": "ольга",
    "оля": "ольга", "ольга": "ольга", "оленька": "ольга",
    "дима": "дмитрий", "дмитрий": "дмитрий", "димон": "дмитрий", "митя": "дмитрий",
    "саша": "александр", "александр": "александр", "александра": "александр",
    "шура": "александр", "алекс": "александр",
    "женя": "евгений", "евгений": "евгений", "евгения": "евгений",
    "катя": "екатерина", "екатерина": "екатерина",
    "настя": "анастасия", "анастасия": "анастасия",
    "лена": "елена", "елена": "елена", "алёна": "елена", "алена": "елена",
    "наташа": "наталья", "наталья": "наталья", "наталия": "наталья",
    "коля": "николай", "николай": "николай",
    "ваня": "иван", "иван": "иван",
    "петя": "пётр", "петр": "пётр", "пётр": "пётр",
    "миша": "михаил", "михаил": "михаил",
    "серёжа": "сергей", "сережа": "сергей", "сергей": "сергей",
    "юля": "юлия", "юлия": "юлия",
    "таня": "татьяна", "татьяна": "татьяна",
    "вика": "виктория", "виктория": "виктория",
    "лиза": "елизавета", "елизавета": "елизавета",
    "соня": "софия", "софия": "софия", "софья": "софия",
    "аня": "анна", "анна": "анна", "анюта": "анна",
    "вова": "владимир", "володя": "владимир", "владимир": "владимир",
    "лёша": "алексей", "леша": "алексей", "алексей": "алексей",
    "паша": "павел", "павел": "павел",
    "рома": "роман", "роман": "роман",
    "костя": "константин", "константин": "константин",
    "макс": "максим", "максим": "максим",
    "даша": "дарья", "дарья": "дарья", "дария": "дарья",
    "ира": "ирина", "ирина": "ирина",
    "света": "светлана", "светлана": "светлана",
    "галя": "галина", "галина": "галина",
    "андрей": "андрей", "андрюша": "андрей",
}

# LAT → RU по звучанию, чтобы Masha/Matia/Olga фолдались в кириллицу и дальше
# через _DIMINUTIVES. Порядок важен: длинные диграфы раньше одиночных букв.
_LAT2RU = [
    ("shch", "щ"), ("sch", "щ"), ("yo", "ё"), ("zh", "ж"), ("kh", "х"),
    ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"),
    ("ia", "я"), ("ja", "я"), ("ju", "ю"), ("ii", "ий"), ("iy", "ий"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"),
    ("g", "г"), ("h", "х"), ("i", "и"), ("j", "й"), ("k", "к"), ("l", "л"),
    ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"),
    ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"),
    ("y", "и"), ("z", "з"),
]

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")


def _fold_lat(token: str) -> str:
    out = token
    for lat, ru in _LAT2RU:
        out = out.replace(lat, ru)
    return out


def canonical_name_parts(name: str | None) -> tuple[str, str]:
    """(canonical_first, canonical_last) — lower, translit-folded to cyrillic,
    diminutives collapsed. Emoji/decorations stripped. '' when absent."""
    tokens = _WORD_RE.findall(name or "")
    if not tokens:
        return "", ""
    first = _fold_lat(tokens[0].lower())
    first = first.replace("ё", "е").replace("й", "и")
    first = _DIMINUTIVES.get(first, first)
    last = _fold_lat(tokens[1].lower()) if len(tokens) > 1 else ""
    last = last.replace("ё", "е").replace("й", "и")
    return first, last


# ─── Candidate pairs ────────────────────────────────────────────────────────


async def find_identity_candidates(limit: int = 12) -> list[tuple[int, int, str]]:
    """(entity_a, entity_b, signal) pairs worth asking Vera about. Ordered:
    cross-source first (the gmail↔telegram unification case), then shared-chat,
    then same-last-name. Pairs already judged (any status) are excluded."""
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT e.id, e.name, e.attributes,
                   (SELECT a.source FROM entity_aliases a
                     WHERE a.entity_id = e.id LIMIT 1) AS src
            FROM entities e WHERE e.type = 'person'
        """))).all()
        member_rows = (await s.execute(text(
            "SELECT child_entity_id, parent_entity_id FROM memberships"
        ))).all()
        judged = {tuple(r) for r in (await s.execute(text(
            "SELECT entity_a, entity_b FROM merge_suggestions"
        ))).all()}

    chats_of: dict[int, set[int]] = {}
    for child, parent in member_rows:
        chats_of.setdefault(child, set()).add(parent)

    by_first: dict[str, list[dict]] = {}
    for eid, name, attrs, src in rows:
        first, last = canonical_name_parts(name)
        if len(first) < 3:
            continue
        by_first.setdefault(first, []).append(
            {"id": eid, "last": last, "src": src or "telegram",
             "attrs": _as_dict(attrs)})

    out: list[tuple[int, int, str, int]] = []   # (a, b, signal, rank)
    for group in by_first.values():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                pa, pb = sorted((a["id"], b["id"]))
                if (pa, pb) in judged:
                    continue
                if a["src"] != b["src"]:
                    out.append((pa, pb, "разные каналы (кросс-источник)", 0))
                elif chats_of.get(a["id"], set()) & chats_of.get(b["id"], set()):
                    out.append((pa, pb, "общие чаты", 1))
                elif a["last"] and a["last"] == b["last"]:
                    out.append((pa, pb, "совпадает фамилия", 2))
    out.sort(key=lambda t: t[3])
    return [(a, b, sig) for a, b, sig, _ in out[:limit]]


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


async def save_suggestion(a_id: int, b_id: int, verdict: dict[str, Any]) -> None:
    """Insert the judged pair. 'different' lands pre-rejected so it is never
    re-asked but also never shown as a pending suggestion."""
    a_id, b_id = sorted((a_id, b_id))
    status = "rejected" if verdict["verdict"] == "different" else "pending"
    async with get_session() as s:
        exists = (await s.execute(text(
            "SELECT 1 FROM merge_suggestions WHERE entity_a=:a AND entity_b=:b"
        ), {"a": a_id, "b": b_id})).first()
        if exists:
            return
        await s.execute(text(
            "INSERT INTO merge_suggestions "
            "(entity_a, entity_b, verdict, confidence, reason, status) "
            "VALUES (:a, :b, :v, :c, :r, :s)"
        ), {"a": a_id, "b": b_id, "v": verdict["verdict"],
            "c": verdict["confidence"], "r": verdict["reason"], "s": status})


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


async def list_pending_suggestions() -> list[dict]:
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT m.id, m.entity_a, m.entity_b, m.verdict, m.confidence, m.reason
            FROM merge_suggestions m
            WHERE m.status = 'pending'
            ORDER BY m.confidence DESC, m.id
        """))).mappings().all()
    return [dict(r) for r in rows]


async def set_suggestion_status(suggestion_id: int, status: str) -> dict | None:
    """Mark accepted/rejected; returns the row (for the merge step) or None."""
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT id, entity_a, entity_b FROM merge_suggestions WHERE id=:i"
        ), {"i": suggestion_id})).mappings().first()
        if row is None:
            return None
        await s.execute(text(
            "UPDATE merge_suggestions SET status=:s WHERE id=:i"
        ), {"s": status, "i": suggestion_id})
    return dict(row)
