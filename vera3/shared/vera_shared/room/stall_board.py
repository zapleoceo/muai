"""Пульт зависших задач владельца: одно сообщение в Telegram, которое правится на месте.

Состояние — одна строка `stall_board` (миграция 063). Каждая отрисовка с новым текстом
поднимает `rev`; номер ревизии уходит в callback-кнопки, и нажатие по устаревшему
пульту ничего не делает (иначе кнопка применилась бы к задаче, которую владелец
уже не видит). Новое сообщение — не чаще `NOTIFY_EVERY`, остальное время только правка.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import StallBoardRow
from vera_shared.room import owner_actions, owner_task_actions
from vera_shared.room.stall import Stalled, stalled_tasks
from vera_shared.room.stall_rules import SNOOZE, days_word, question_text
from vera_shared.room.tasks import TaskBusy, TaskNotFound
from vera_shared.timeutil import utc_naive_now

BOARD = "owner"
NOTIFY_EVERY = timedelta(hours=24)
PREFIX = "sb:"
VIA = "telegram"
ASK, CONFIRM = "ask", "confirm_cancel"
EMPTY_TEXT = "Зависших задач нет."
CONFIRM_TEXT = "Отменить задачу насовсем? Взять её снова будет нельзя."
TOASTS = {"c": "Вернул в работу", "w": f"Отложил на {SNOOZE.days} {days_word(SNOOZE.days)}", "p": "Поставил на паузу",
          "y": "Отменил", "n": "Следующая", "x": "Подтвердите отмену", "b": "Назад"}
REASONS = {"c": "Продолжить: кнопка пульта", "w": "Ждать: кнопка пульта",
           "p": "Пауза: кнопка пульта", "y": "Отмена: кнопка пульта"}


@dataclass(frozen=True)
class Plan:
    action: str  # none | send | edit
    text: str = ""
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    message_id: int | None = None


def key_of(t: Stalled) -> str:
    return f"{t.room}/{t.task_id}"


def pick_current(stalled: list[Stalled], current: str | None, skipped: list[str]) -> Stalled | None:
    """Текущая, если ещё висит; иначе первая непропущенная; все пропущены — снова с начала."""
    by_key = {key_of(t): t for t in stalled}
    if current in by_key:
        return by_key[current]
    return next((t for t in stalled if key_of(t) not in skipped), stalled[0] if stalled else None)


def render(stalled: list[Stalled], current: Stalled | None, mode: str,
           rev: int) -> tuple[str, list[list[tuple[str, str]]]]:
    if current is None:
        return EMPTY_TEXT, []
    pos = [key_of(t) for t in stalled].index(key_of(current)) + 1
    body = question_text(title=current.title, task_id=current.task_id, project=current.project,
                         status=current.status, note=current.note,
                         next_action=current.next_action, idle=current.idle)
    text = f"Зависшие задачи: {len(stalled)} (сейчас {pos} из {len(stalled)})\n\n{body}"

    def btn(label: str, act: str) -> tuple[str, str]:
        return label, f"{PREFIX}{act}:{rev}"

    if mode == CONFIRM:
        return f"{text}\n\n{CONFIRM_TEXT}", [[btn("Да, отменить", "y"), btn("Назад", "b")]]
    rows = [[btn("Продолжить", "c"), btn(f"Ждать {SNOOZE.days} {days_word(SNOOZE.days)}", "w")],
            [btn("Пауза", "p"), btn("Отменить", "x")]]
    if len(stalled) > 1:
        rows.append([btn("Следующая", "n")])
    return text, rows


async def _state(s) -> StallBoardRow:
    row = await s.get(StallBoardRow, BOARD, with_for_update=True)
    if row is None:
        row = StallBoardRow(name=BOARD, rev=0, mode=ASK, skipped=[])
        s.add(row)
        await s.flush()
    return row


async def plan(now: datetime | None = None) -> Plan:
    """Что сделать с сообщением сейчас. Состояние сохраняется сразу; отправку делает вызывающий."""
    now = now or utc_naive_now()
    stalled = await stalled_tasks(now)
    async with get_session() as s:
        st = await _state(s)
        fresh = bool(stalled) and (st.message_id is None or st.posted_at is None
                                   or now - st.posted_at >= NOTIFY_EVERY)
        if fresh:
            st.skipped, st.mode = [], ASK
        current = pick_current(stalled, st.current_task_id and
                               f"{st.current_room}/{st.current_task_id}", list(st.skipped or []))
        if current is None or (st.current_room, st.current_task_id) != (current.room, current.task_id):
            st.mode = ASK
        text, _ = render(stalled, current, st.mode, st.rev)
        if not fresh and (st.message_id is None or text == st.rendered):
            return Plan("none")
        st.rev += 1
        text, buttons = render(stalled, current, st.mode, st.rev)
        st.current_room = current.room if current else None
        st.current_task_id = current.task_id if current else None
        st.rendered, st.updated_at = text, now
        return Plan("send" if fresh else "edit", text, buttons, st.message_id)


async def remember_sent(message_id: int, now: datetime | None = None) -> None:
    async with get_session() as s:
        st = await _state(s)
        st.message_id, st.posted_at = message_id, now or utc_naive_now()


async def forget_message() -> None:
    """Сообщение пропало в Telegram (удалено владельцем): следующий проход пришлёт новое."""
    async with get_session() as s:
        st = await _state(s)
        st.message_id, st.posted_at, st.rendered = None, None, None


async def press(data: str) -> str:
    """Нажатие кнопки `sb:<действие>:<rev>`. Возвращает короткий ответ для всплывашки."""
    try:
        act, rev_s = data.removeprefix(PREFIX).split(":", 1)
        rev = int(rev_s)
    except ValueError:
        return "Неизвестная кнопка"
    if act not in TOASTS:
        return "Неизвестная кнопка"
    async with get_session() as s:
        st = await _state(s)
        if rev != st.rev or not st.current_task_id:
            return "Пульт обновился, посмотрите ещё раз"
        room, task_id = st.current_room or "main", st.current_task_id
        if act in ("x", "b"):
            st.mode = CONFIRM if act == "x" else ASK
            st.rendered = None  # заставить перерисовку
            return TOASTS[act]
        if act == "n":
            st.skipped = [*(st.skipped or []), f"{room}/{task_id}"]
            st.current_room, st.current_task_id, st.mode, st.rendered = None, None, ASK, None
            return TOASTS[act]
        if act == "y" and st.mode != CONFIRM:
            return "Сначала нажмите \"Отменить\""
        st.mode, st.rendered = ASK, None
    try:
        await _apply(act, room, task_id)
    except (TaskBusy, TaskNotFound, ValueError) as e:
        return f"Не получилось: {str(e)[:150]}"
    return TOASTS[act]


async def _apply(act: str, room: str, task_id: str) -> None:
    reason = REASONS[act]
    if act == "c":
        await owner_task_actions.continue_by_owner(room=room, task_id=task_id, reason=reason, via=VIA)
    elif act == "w":
        await owner_task_actions.snooze_by_owner(room=room, task_id=task_id, reason=reason, via=VIA)
    elif act == "p":
        await owner_actions.pause_by_owner(room=room, task_id=task_id, reason=reason, via=VIA)
    elif act == "y":
        await owner_task_actions.cancel_by_owner(room=room, task_id=task_id, reason=reason, via=VIA)
