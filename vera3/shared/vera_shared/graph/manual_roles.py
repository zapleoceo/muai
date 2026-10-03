"""Связь, указанная владельцем вручную («Указать связь» в карточке дашборда).

Роль выбирается из короткого списка и описывается от лица пары (X — смотрящая
карточка, Y — с кем связываем): ключ → (предикат, X — субъект?). В базу уходит
КАНОНИЧЕСКАЯ форма (`rel_canon.canonical_edge`: симметричные — меньший id первым,
обратные пары — активной формой), строка ручная: без события-источника, с
уверенностью 1.0 — модель связи даёт ей максимальный вес. Путь записи тот же,
что у MCP `relationship_set` (`graph.edit.set_relationship` + `mcp_audit`), клиент
в журнале — `dashboard`, откат — общий `journal.undo`.
"""
from __future__ import annotations

from vera_shared.db.engine import get_session
from vera_shared.graph import edit as graph_edit
from vera_shared.graph.edit import GraphEditError
from vera_shared.graph.rel_canon import canonical_edge
from vera_shared.journal import audit

#: ключ → (предикат, X — субъект предиката)
MANUAL_ROLES: dict[str, tuple[str, bool]] = {
    "x_boss_of_y": ("boss_of", True),
    "y_boss_of_x": ("boss_of", False),
    "coworkers": ("coworker_of", True),
    "friends": ("friend_of", True),
    "spouses": ("spouse_of", True),
    "x_parent_of_y": ("parent_of", True),
    "y_parent_of_x": ("parent_of", False),
    "x_client_of_y": ("client_of", True),
    "y_client_of_x": ("client_of", False),
    "x_vendor_of_y": ("vendor_of", True),
    "y_vendor_of_x": ("vendor_of", False),
}
MANUAL_FACT = "Указано владельцем"
MANUAL_CONFIDENCE = 1.0


async def set_manual_role(viewed_id: int, anchor_id: int, role: str, client: str) -> int:
    """Записывает связь и строку журнала одной транзакцией; возвращает id строки журнала."""
    if role not in MANUAL_ROLES:
        raise GraphEditError(f"unknown role '{role}'; one of {sorted(MANUAL_ROLES)}")
    predicate, viewed_is_subject = MANUAL_ROLES[role]
    subject, obj = (viewed_id, anchor_id) if viewed_is_subject else (anchor_id, viewed_id)
    subject, predicate, obj = canonical_edge(subject, predicate, obj)
    async with get_session() as s:
        rel_id, before, after = await graph_edit.set_relationship(
            s, subject, obj, predicate, MANUAL_FACT, MANUAL_CONFIDENCE, manual=True)
        return await audit.record(
            s, client=client, tool="relationship_set",
            args={"subject_id": subject, "object_id": obj, "predicate": predicate,
                  "fact": MANUAL_FACT, "confidence": MANUAL_CONFIDENCE},
            kind="relationship", target_id=rel_id, before=before, after=after)
