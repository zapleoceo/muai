"""Решение «писать ли связь» при извлечении: правила, а для одиночных имён — модель."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from vera_shared.graph.rel_text import End, Evidence
from vera_shared.graph.rel_validate import (
    REJECT_SELF,
    REJECT_WEAK_NAME,
    relationship_reject_reason,
)
from vera_shared.graph.rel_verify import YES, Cache, EdgeQuery, verify_many
from vera_shared.ingest.envelope import message_body

log = logging.getLogger(__name__)

EntityBrief = tuple[int, str, str]   # (id, имя, тип)

# Воркер триажа не должен зависнуть на минуты: проверяются не больше трёх связей
# на сообщение (столько и отдаёт схема rel_extract), ждём ответ не дольше 20 с,
# остальные отклоняются как weak_name — безопасная сторона.
MAX_JUDGED_PER_MESSAGE = 3
POLL_DEADLINE_S = 20.0
_MAX_CACHED = 5000
_cache: Cache = {}


@dataclass(frozen=True)
class Candidate:
    ends: tuple[EntityBrief, EntityBrief]
    predicate: str
    confidence: float
    evidence: Evidence


def _rules(c: Candidate, evidence: Evidence | None = None) -> str | None:
    (subj_id, subj_name, subj_type), (obj_id, obj_name, obj_type) = c.ends
    if subj_id == obj_id:
        return REJECT_SELF
    return relationship_reject_reason(
        subject_name=subj_name, subject_type=subj_type, predicate=c.predicate,
        object_name=obj_name, object_type=obj_type, confidence=c.confidence,
        evidence=evidence or c.evidence)


def _as_verified(evidence: Evidence) -> Evidence:
    subject, obj = evidence.subject, evidence.object
    return Evidence(evidence.fact, End(subject.names, True, subject.author),
                    End(obj.names, True, obj.author))


async def judge_batch(candidates: list[Candidate], *, event_id: int,
                      body: str) -> list[str | None]:
    """Причины отказа (None — писать) в порядке `candidates`.

    Персона из одного слова проходит, если модель находит в тексте прямое
    утверждение (цитата проверена); остальные проверки при этом сохраняются.
    Вердикты считаются одной пачкой с ограниченной очередью и кэшируются."""
    reasons = [_rules(c) for c in candidates]
    weak = [i for i, r in enumerate(reasons) if r == REJECT_WEAK_NAME]
    judged, skipped = weak[:MAX_JUDGED_PER_MESSAGE], weak[MAX_JUDGED_PER_MESSAGE:]
    if skipped:
        log.warning("rel_judge event=%s: %d связей сверх лимита %d отклонены как weak_name",
                    event_id, len(skipped), MAX_JUDGED_PER_MESSAGE)
    if not judged:
        return reasons
    if len(_cache) > _MAX_CACHED:
        _cache.clear()
    text = message_body(body)
    queries = [EdgeQuery(event_id, candidates[i].ends[0][1], candidates[i].predicate,
                         candidates[i].ends[1][1]) for i in judged]
    verdicts = await verify_many(((q, text) for q in queries), cache=_cache,
                                 poll_deadline_s=POLL_DEADLINE_S)
    for i, verdict in zip(judged, verdicts, strict=True):
        if verdict.verdict == YES:
            reasons[i] = _rules(candidates[i], _as_verified(candidates[i].evidence))
    return reasons
