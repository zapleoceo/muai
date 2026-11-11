"""Бот и пульт: одно сообщение, правка вместо новых, повторная отправка только если пропало."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from bot_telegram import stall_worker
from vera_shared.room import stall_board

pytestmark = pytest.mark.asyncio
OWNER = 169


class FakeBot:
    def __init__(self, edit_error: str | None = None) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.edited: list[int] = []
        self.deleted: list[int] = []
        self.edit_error = edit_error

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=500 + len(self.sent))

    async def edit_message_text(self, text, chat_id, message_id, reply_markup=None):
        if self.edit_error:
            raise TelegramBadRequest(method=None, message=self.edit_error)
        self.edited.append(message_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


def plan(action: str, message_id: int | None = None) -> stall_board.Plan:
    return stall_board.Plan(action, "Зависшие <задачи>", [[("Пауза", "sb:p:1")]], message_id)


@pytest.fixture
def board(monkeypatch):
    state = {"plan": plan("none"), "sent": [], "forgot": 0}

    async def fake_plan():
        return state["plan"]

    async def remember(message_id):
        state["sent"].append(message_id)

    async def forget():
        state["forgot"] += 1

    monkeypatch.setattr(stall_board, "plan", fake_plan)
    monkeypatch.setattr(stall_board, "remember_sent", remember)
    monkeypatch.setattr(stall_board, "forget_message", forget)
    return state


async def test_nothing_to_do_sends_nothing(board):
    bot = FakeBot()
    assert await stall_worker.sync(bot, OWNER) == "none"
    assert bot.sent == [] and bot.edited == []


async def test_send_escapes_text_attaches_buttons_and_drops_the_old_board(board):
    board["plan"] = plan("send", message_id=42)
    bot = FakeBot()
    assert await stall_worker.sync(bot, OWNER) == "send"
    chat, text, markup = bot.sent[0]
    assert chat == OWNER and text == "Зависшие &lt;задачи&gt;"
    assert markup.inline_keyboard[0][0].callback_data == "sb:p:1"
    assert bot.deleted == [42] and board["sent"] == [501]


async def test_edit_changes_the_same_message_without_a_new_one(board):
    board["plan"] = plan("edit", message_id=42)
    bot = FakeBot()
    assert await stall_worker.sync(bot, OWNER) == "edit"
    assert bot.edited == [42] and bot.sent == []


async def test_unchanged_message_is_not_an_error(board):
    board["plan"] = plan("edit", message_id=42)
    bot = FakeBot("Bad Request: message is not modified")
    assert await stall_worker.sync(bot, OWNER) == "edit"
    assert bot.sent == []


async def test_deleted_message_is_forgotten_and_sent_again(board):
    board["plan"] = plan("edit", message_id=42)
    bot = FakeBot("Bad Request: message to edit not found")
    assert await stall_worker.sync(bot, OWNER) == "send"
    assert board["forgot"] == 1 and len(bot.sent) == 1


async def test_other_telegram_errors_surface(board):
    board["plan"] = plan("edit", message_id=42)
    with pytest.raises(TelegramBadRequest):
        await stall_worker.sync(FakeBot("Bad Request: chat not found"), OWNER)


async def test_refuses_to_send_without_an_owner(board):
    with pytest.raises(RuntimeError):
        await stall_worker.sync(FakeBot(), 0)


async def test_press_applies_then_redraws(board, monkeypatch):
    async def fake_press(data):
        return f"ok:{data}"

    monkeypatch.setattr(stall_board, "press", fake_press)
    board["plan"] = plan("edit", message_id=42)
    bot = FakeBot()
    assert await stall_worker.press(bot, OWNER, "sb:c:1") == "ok:sb:c:1"
    assert bot.edited == [42]


async def test_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("STALL_BOARD_ENABLED", raising=False)
    assert stall_worker.enabled() is False
    monkeypatch.setenv("STALL_BOARD_ENABLED", "1")
    assert stall_worker.enabled() is True
    assert stall_worker.markup([]) is None
