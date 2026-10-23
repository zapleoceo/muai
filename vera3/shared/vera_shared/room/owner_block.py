"""Блок владельца (OB, дизайн §2.7): вопрос, ожидание владельца, пауза владельца.

Исполнитель не может снять OB сам: снимают только события разрешения владельца
или подтверждение ответа. Здесь — предикаты и отказ; запись событий разрешения
(маршруты владельца) сюда не входит.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models_room import RoomTaskEventRow, RoomTaskQuestionRow, RoomTaskRow

OWNER_LABELS = frozenset({"owner"})
BY_HOLDER, BY_OWNER = "holder", "owner"
PAUSE_KINDS = ("paused", "resumed")


class OwnerBlock(RuntimeError):
    """Действие снимало бы блок владельца, а снять его может только владелец."""


@dataclass(frozen=True)
class OwnerBlockState:
    question: bool
    wait: bool
    pause: bool

    @property
    def active(self) -> bool:
        return self.question or self.wait or self.pause

    def reasons(self) -> list[str]:
        return [name for name, on in (("owner_question", self.question),
                                      ("owner_wait", self.wait),
                                      ("owner_pause", self.pause)) if on]


def owner_wait(row: RoomTaskRow) -> bool:
    return (row.waiting_decision_owner or "") in OWNER_LABELS


def clear_all_wait(row: RoomTaskRow) -> None:
    """Обнуляет поля ожидания целиком; зовут только события владельца (wait/resolve, start)."""
    row.waiting_until, row.waiting_reason, row.waiting_decision_owner = None, None, None


def pause_actor(data: dict[str, Any] | None) -> str:
    """Легаси paused/resumed без data.by пишет только set_state под арендой — это держатель."""
    by = (data or {}).get("by")
    return BY_OWNER if by == BY_OWNER else BY_HOLDER


def fold_pauses(events: Iterable[tuple[str, dict[str, Any] | None]]) -> dict[str, bool]:
    """События paused/resumed по возрастанию id -> {holder: на паузе, owner: на паузе}."""
    state = {BY_HOLDER: False, BY_OWNER: False}
    for kind, data in events:
        if kind in PAUSE_KINDS:
            state[pause_actor(data)] = kind == "paused"
    return state


async def pause_state(s: AsyncSession, room: str, task_id: str) -> dict[str, bool]:
    rows = (await s.execute(select(RoomTaskEventRow.kind, RoomTaskEventRow.data).where(
        RoomTaskEventRow.room == room, RoomTaskEventRow.task_id == task_id,
        RoomTaskEventRow.kind.in_(PAUSE_KINDS)).order_by(RoomTaskEventRow.id))).all()
    return fold_pauses((kind, data) for kind, data in rows)


async def unresolved_questions(s: AsyncSession, room: str, task_id: str) -> int:
    """Вопросы в open (ждут владельца) и answered (ждут подтверждения исполнителем)."""
    return (await s.execute(select(func.count()).select_from(RoomTaskQuestionRow).where(
        RoomTaskQuestionRow.room == room, RoomTaskQuestionRow.task_id == task_id,
        RoomTaskQuestionRow.status.in_(("open", "answered"))))).scalar() or 0


async def owner_block(s: AsyncSession, row: RoomTaskRow) -> OwnerBlockState:
    return OwnerBlockState(
        question=await unresolved_questions(s, row.room, row.task_id) > 0,
        wait=owner_wait(row),
        pause=(await pause_state(s, row.room, row.task_id))[BY_OWNER])


async def require_no_owner_block(s: AsyncSession, row: RoomTaskRow, action: str,
                                 ) -> None:
    state = await owner_block(s, row)
    if state.active:
        raise OwnerBlock(f"{action}: task {row.task_id!r} awaits the owner "
                         f"({', '.join(state.reasons())}); only the owner's resolution "
                         "event lifts it")
