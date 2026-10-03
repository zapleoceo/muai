"""Решение «писать ли связь» при извлечении: правила, а для одиночных имён — модель."""
from __future__ import annotations

from vera_shared.graph.rel_text import End, Evidence
from vera_shared.graph.rel_validate import (
    REJECT_SELF,
    REJECT_WEAK_NAME,
    relationship_reject_reason,
)
from vera_shared.graph.rel_verify import YES, EdgeQuery, verify_edge
from vera_shared.ingest.envelope import message_body

EntityBrief = tuple[int, str, str]   # (id, имя, тип)


def _rules(ends: tuple[EntityBrief, EntityBrief], predicate: str, confidence: float,
           evidence: Evidence) -> str | None:
    (subj_id, subj_name, subj_type), (obj_id, obj_name, obj_type) = ends
    if subj_id == obj_id:
        return REJECT_SELF
    return relationship_reject_reason(
        subject_name=subj_name, subject_type=subj_type, predicate=predicate,
        object_name=obj_name, object_type=obj_type, confidence=confidence,
        evidence=evidence)


async def judge_relationship(
    ends: tuple[EntityBrief, EntityBrief], predicate: str, confidence: float,
    evidence: Evidence, *, event_id: int, body: str,
) -> str | None:
    """Причина отказа или None. Персона из одного слова проходит, если модель
    находит в тексте прямое утверждение (цитата проверена); дорогой вызов —
    только для таких связей, остальные проверки при этом сохраняются."""
    reason = _rules(ends, predicate, confidence, evidence)
    if reason != REJECT_WEAK_NAME:
        return reason
    query = EdgeQuery(event_id, ends[0][1], predicate, ends[1][1])
    if (await verify_edge(query, message_body(body))).verdict != YES:
        return reason
    subject, obj = evidence.subject, evidence.object
    verified = Evidence(evidence.fact, End(subject.names, True, subject.author),
                        End(obj.names, True, obj.author))
    return _rules(ends, predicate, confidence, verified)
