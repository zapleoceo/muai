"""Предложение слияния от кода (не от LLM-судьи): пара на ручной разбор.

Лёгкая зависимость — только ORM, без `identity`/`dedup`: ею пользуется
ингест-ядро, а `dedup` само импортирует ингест (иначе круг).
"""
from __future__ import annotations

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import MergeSuggestionRow


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
