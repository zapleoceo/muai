"""Ответ владельца на «Это ты сказал?» — разбор кнопки и переход в очереди.

Отдельно от `bot.py`, чтобы проверялось без Telegram: кнопка несёт
`vh:y:<command_id>` или `vh:n:<command_id>` (≤ 64 байт — command_id от
слушателя 19 символов). Кто нажал, проверяет `bot.on_help_confirmation`.
"""
from __future__ import annotations

from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.queue_state import answer_confirmation

CALLBACK_PREFIX = "vh:"

REPLIES = {
    "confirmed": "Принято — завожу срочную задачу.",
    "declined": "Хорошо, ничего не делаю.",
    "expired": "Поздно: прошло больше 10 минут, просьба отменена. Скажи ещё раз.",
    "unknown": "Эта просьба уже решена.",
}


def parse_callback(data: str) -> tuple[str, bool] | None:
    """→ (command_id, «Да» ли это) или None, если кнопка не наша."""
    if not data.startswith(CALLBACK_PREFIX):
        return None
    answer, _, command_id = data[len(CALLBACK_PREFIX):].partition(":")
    if answer not in ("y", "n") or not command_id:
        return None
    return command_id, answer == "y"


async def handle_confirmation(command_id: str, yes: bool) -> str:
    """→ текст всплывающего ответа на нажатие."""
    return REPLIES[await answer_confirmation(command_id, yes, utc_naive_now())]
