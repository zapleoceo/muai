"""Пульт зависших задач: одно сообщение владельцу, правится на месте раз в `INTERVAL_S`.

Новое сообщение (со звуком) — только когда `stall_board.plan` просит `send`: не чаще раза
в сутки, и только если есть зависшие задачи; прежнее при этом удаляется, чтобы в чате
оставался один пульт. Цикл и нажатия кнопок делят один замок — иначе два одновременных
прохода могли бы прислать два пульта.
"""
from __future__ import annotations

import asyncio
import logging
import os
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from vera_shared.room import stall_board

log = logging.getLogger(__name__)
INTERVAL_S = 300
_lock = asyncio.Lock()
_GONE = ("message to edit not found", "message can't be edited", "message_id_invalid")


def enabled() -> bool:
    return os.environ.get("STALL_BOARD_ENABLED", "0") == "1"


def markup(buttons: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in buttons])


async def _edit(bot: Bot, owner_id: int, plan: stall_board.Plan) -> bool:
    """True — сообщение на месте (исправлено или уже такое). False — его больше нет."""
    try:
        await bot.edit_message_text(escape(plan.text, quote=False), chat_id=owner_id,
                                    message_id=plan.message_id, reply_markup=markup(plan.buttons))
        return True
    except TelegramBadRequest as e:
        text = str(e).lower()
        if "not modified" in text:
            return True
        if any(marker in text for marker in _GONE):
            await stall_board.forget_message()
            return False
        raise


async def _send(bot: Bot, owner_id: int, plan: stall_board.Plan) -> None:
    if plan.message_id:
        try:
            await bot.delete_message(owner_id, plan.message_id)
        except TelegramBadRequest:
            log.info("stall board: previous message already gone")
    sent = await bot.send_message(owner_id, escape(plan.text, quote=False),
                                  reply_markup=markup(plan.buttons))
    await stall_board.remember_sent(sent.message_id)


async def sync(bot: Bot, owner_id: int) -> str:
    """Один проход: привести сообщение к текущему состоянию. Возвращает, что сделано."""
    if owner_id == 0:
        raise RuntimeError("OWNER_TELEGRAM_ID not set — refusing to send")
    async with _lock:
        plan = await stall_board.plan()
        if plan.action == "none":
            return "none"
        if plan.action == "edit" and plan.message_id and await _edit(bot, owner_id, plan):
            return "edit"
        await _send(bot, owner_id, plan)
        return "send"


async def press(bot: Bot, owner_id: int, data: str) -> str:
    toast = await stall_board.press(data)
    await sync(bot, owner_id)
    return toast


async def run_forever(bot: Bot, owner_id: int, interval_s: float = INTERVAL_S) -> None:
    while True:
        try:
            await sync(bot, owner_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("stall board pass failed")
        await asyncio.sleep(interval_s)
