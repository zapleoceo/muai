"""Предложение слияния от кода (не от LLM-судьи): пара на ручной разбор.

Лёгкая зависимость — только ORM, без `identity`/`dedup`: ею пользуется
ингест-ядро, а `dedup` само импортирует ингест (иначе круг).
"""
from __future__ import annotations

from sqlalchemy import func, select

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import MergeSuggestionRow


async def count_pending_suggestions() -> int:
    """Сколько пар ждёт решения владельца — число для ссылки «Дубли (N)»."""
    async with get_session() as s:
        return int((await s.execute(
            select(func.count()).select_from(MergeSuggestionRow)
            .where(MergeSuggestionRow.status == "pending"))).scalar_one())


async def propose_merge(entity_a: int, entity_b: int, *, confidence: float,
                        reason: str) -> bool:
    """Записать пару как pending. Уже осуждённая (любой статус) не переспрашивается.
    → True, если строка создана."""
    a, b = sorted((entity_a, entity_b))
    if a == b:
        return False
    async with get_session() as s:
        exists = (await s.execute(select(MergeSuggestionRow.id).where(
            MergeSuggestionRow.entity_a == a,
            MergeSuggestionRow.entity_b == b))).first()
        if exists:
            return False
        s.add(MergeSuggestionRow(entity_a=a, entity_b=b, verdict="same",
                                 confidence=confidence, reason=reason,
                                 status="pending"))
    return True


async def reject_pair(entity_a: int, entity_b: int, reason: str) -> None:
    """«Разные люди» для пары без предложения (точное совпадение идентификатора): записать
    её отклонённой, чтобы очередь не показывала её снова. Есть строка — только меняет статус."""
    a, b = sorted((entity_a, entity_b))
    if a == b:
        return
    async with get_session() as s:
        row = (await s.execute(select(MergeSuggestionRow).where(
            MergeSuggestionRow.entity_a == a, MergeSuggestionRow.entity_b == b))).scalar_one_or_none()
        if row is None:
            s.add(MergeSuggestionRow(entity_a=a, entity_b=b, verdict="different",
                                     confidence=1.0, reason=reason, status="rejected"))
        else:
            row.status = "rejected"


async def list_decided_pairs() -> set[tuple[int, int]]:
    """Пары, по которым уже есть решение (принято/отклонено): очередь их не повторяет."""
    async with get_session() as s:
        rows = (await s.execute(select(MergeSuggestionRow.entity_a, MergeSuggestionRow.entity_b)
                                .where(MergeSuggestionRow.status != "pending"))).all()
    return {tuple(r) for r in rows}
