import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.sender import get_bot
from app.config import get_settings
from app.triage.engine import TriageProposal

log = logging.getLogger(__name__)

_URGENCY_ICON = {
    "low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴",
}


def _build_text(event_id: int, source: str, category: str, proposal: TriageProposal) -> str:
    icon = _URGENCY_ICON.get(proposal.urgency, "⚪")
    lines = [
        f"{icon} <b>{source}</b> · <code>{category}</code> · #{event_id}",
        "",
        f"<b>{_html_escape(proposal.summary)}</b>",
    ]
    if proposal.reasoning:
        lines.append("")
        lines.append(f"<i>{_html_escape(proposal.reasoning)}</i>")
    if proposal.context_used:
        lines.append("")
        lines.append("<b>Контекст:</b>")
        for f in proposal.context_used[:3]:
            lines.append(f"• {_html_escape(f[:200])}")
    lines.append("")
    lines.append(f"Уверенность: {int(proposal.confidence * 100)}%")
    return "\n".join(lines)


def _build_keyboard(event_id: int, proposal: TriageProposal) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, a in enumerate(proposal.actions):
        prefix = "⭐ " if a.get("default") else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{prefix}{a['label']}",
                callback_data=f"tri:{event_id}:{idx}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="💬 Свой ответ…", callback_data=f"tri:{event_id}:custom"),
        InlineKeyboardButton(text="🙈 Игнорить",   callback_data=f"tri:{event_id}:ignore"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def send_card(event_id: int, source: str, category: str,
                    proposal: TriageProposal, auto_note: str | None = None) -> int | None:
    settings = get_settings()
    bot = get_bot()
    text = _build_text(event_id, source, category, proposal)
    if auto_note:
        text += "\n\n" + auto_note
    kb = None if auto_note else _build_keyboard(event_id, proposal)
    try:
        msg = await bot.send_message(
            chat_id=settings.vera_group_id,
            text=text, parse_mode="HTML", reply_markup=kb,
        )
        return msg.message_id
    except TelegramBadRequest as exc:
        log.warning("Telegram send_card failed: %s", exc)
        return None
