import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.triage.dispatcher import record_user_decision

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(lambda c: c.data and c.data.startswith("tri:"))
async def triage_callback(callback: CallbackQuery, bot: Bot) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Битый callback")
        return
    _, event_id_s, choice = parts
    try:
        event_id = int(event_id_s)
    except ValueError:
        await callback.answer("Битый id")
        return

    chosen = await record_user_decision(event_id, choice)
    if chosen is None:
        await callback.answer("Не удалось записать решение")
        return

    label = chosen.get("label", "?")
    await callback.answer(f"✅ {label}")
    try:
        if callback.message:
            base = callback.message.html_text or callback.message.text or ""
            new_text = base + f"\n\n<b>✓ Решено:</b> {_html_escape(label)}"
            await callback.message.edit_text(new_text, parse_mode="HTML", reply_markup=None)
    except TelegramBadRequest as exc:
        log.warning("Failed to edit card: %s", exc)


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
