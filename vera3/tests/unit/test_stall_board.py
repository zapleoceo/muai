"""Пульт зависших задач: одно сообщение, правка на месте, кнопки с ревизией."""
from __future__ import annotations

from datetime import timedelta

import pytest
from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow, StallBoardRow
from vera_shared.room import stall_board, tasks
from vera_shared.room.owner_block import BY_OWNER, pause_state
from vera_shared.timeutil import utc_naive_now

pytestmark = pytest.mark.asyncio
R = "main"


async def make(task_id: str, days: int) -> None:
    await tasks.open_task(room=R, task_id=task_id, agent="dot", title=f"Задача {task_id}",
                          paths=None, project="Lamas")
    async with get_session() as s:
        row = await s.get(RoomTaskRow, (R, task_id))
        row.status = "blocked"
        row.updated_at = row.created_at = utc_naive_now() - timedelta(days=days)


async def status(task_id: str) -> str:
    async with get_session() as s:
        return (await s.get(RoomTaskRow, (R, task_id))).status


async def board() -> StallBoardRow:
    async with get_session() as s:
        return await s.get(StallBoardRow, stall_board.BOARD)


def data(plan: stall_board.Plan, act: str) -> str:
    return next(d for row in plan.buttons for _, d in row if d.startswith(f"sb:{act}:"))


async def sent_board() -> stall_board.Plan:
    plan = await stall_board.plan()
    await stall_board.remember_sent(777)
    return plan


async def test_nothing_stalled_and_no_message_means_silence(sqlite_db):
    assert (await stall_board.plan()).action == "none"


async def test_first_board_is_a_new_message_then_quiet_until_something_changes(sqlite_db):
    await make("A", 9)
    await make("B", 4)
    first = await sent_board()
    assert first.action == "send" and first.message_id is None
    assert first.text.startswith("Зависшие задачи: 2 (сейчас 1 из 2)")
    assert "Задача A" in first.text  # самая давняя первой
    labels = [label for row in first.buttons for label, _ in row]
    assert labels == ["Продолжить", "Ждать 3 дня", "Пауза", "Отменить", "Следующая"]
    assert (await stall_board.plan()).action == "none"


async def test_next_edits_the_same_message_to_the_next_task(sqlite_db):
    await make("A", 9)
    await make("B", 4)
    first = await sent_board()
    assert await stall_board.press(data(first, "n")) == "Следующая"
    nxt = await stall_board.plan()
    assert nxt.action == "edit" and nxt.message_id == 777 and "Задача B" in nxt.text


async def test_stale_button_does_nothing(sqlite_db):
    await make("A", 9)
    first = await sent_board()
    old = data(first, "c")
    await stall_board.press(data(first, "x"))
    await stall_board.plan()  # пульт перерисован, ревизия выросла
    assert await stall_board.press(old) == "Пульт обновился, посмотрите ещё раз"
    assert await status("A") == "blocked"


async def test_cancel_needs_a_confirmation_step(sqlite_db):
    await make("A", 9)
    first = await sent_board()
    assert await stall_board.press(f"sb:y:{(await board()).rev}") == 'Сначала нажмите "Отменить"'
    assert await stall_board.press(data(first, "x")) == "Подтвердите отмену"
    assert await status("A") == "blocked"
    confirm = await stall_board.plan()
    assert confirm.action == "edit" and "Отменить задачу насовсем?" in confirm.text
    assert [label for row in confirm.buttons for label, _ in row] == ["Да, отменить", "Назад"]
    assert await stall_board.press(data(confirm, "y")) == "Отменил"
    assert await status("A") == "cancelled"
    assert (await stall_board.plan()).text == stall_board.EMPTY_TEXT


async def test_continue_and_pause_buttons_move_the_task(sqlite_db):
    await make("A", 9)
    await make("B", 8)
    first = await sent_board()
    assert await stall_board.press(data(first, "c")) == "Вернул в работу"
    assert await status("A") == "open"
    second = await stall_board.plan()
    assert "Задача B" in second.text
    assert await stall_board.press(data(second, "p")) == "Поставил на паузу"
    async with get_session() as s:
        assert (await pause_state(s, R, "B"))[BY_OWNER] is True


async def test_a_new_message_at_most_once_a_day(sqlite_db):
    await make("A", 9)
    await sent_board()
    async with get_session() as s:
        (await s.get(StallBoardRow, stall_board.BOARD)).posted_at = (
            utc_naive_now() - stall_board.NOTIFY_EVERY - timedelta(minutes=1))
    again = await stall_board.plan()
    assert again.action == "send" and again.message_id == 777  # прежнее удалит вызывающий


async def test_lost_message_is_forgotten_and_resent(sqlite_db):
    await make("A", 9)
    await sent_board()
    await stall_board.forget_message()
    assert (await stall_board.plan()).action == "send"


async def test_garbage_button_is_refused(sqlite_db):
    assert await stall_board.press("sb:zz") == "Неизвестная кнопка"
    assert await stall_board.press("sb:q:1") == "Неизвестная кнопка"
