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


from app.common.text import html_escape as _html_escape  # noqa: E402 (kept for stable import)


def _build_auto_kb(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✋ Откати", callback_data=f"tri:{event_id}:undo"),
    ]])


def _build_auto_text(event_id: int, source: str, action_label: str,
                      tool_result_preview: str, confidence: float, ok: bool) -> str:
    icon = "✅" if ok else "⚠️"
    lines = [
        f"{icon} <b>Сделала автоматом:</b> {_html_escape(action_label)}",
        f"<i>{source} · #{event_id} · уверенность {int(confidence*100)}%</i>",
    ]
    if tool_result_preview:
        lines.append(f"<code>{_html_escape(tool_result_preview[:200])}</code>")
    return "\n".join(lines)


async def send_card(event_id: int, source: str, category: str,
                    proposal: TriageProposal, auto_exec: dict | None = None,
                    auto_note: str | None = None) -> int | None:
    """Two render modes:
      - Manual (no auto_exec): full card with summary, reasoning, buttons.
      - Auto (auto_exec set): one-liner + Откати-button. No noise."""
    settings = get_settings()
    bot = get_bot()
    if auto_exec is not None:
        text = _build_auto_text(
            event_id, source,
            (auto_exec.get("label") or "—"),
            str(auto_exec.get("result_preview") or "")[:200],
            proposal.confidence, bool(auto_exec.get("ok")),
        )
        kb = _build_auto_kb(event_id)
    else:
        text = _build_text(event_id, source, category, proposal)
        if auto_note:
            text += "\n\n" + auto_note
        kb = _build_keyboard(event_id, proposal)
    try:
        msg = await bot.send_message(
            chat_id=settings.vera_group_id,
            text=text, parse_mode="HTML", reply_markup=kb,
        )
        return msg.message_id
    except TelegramBadRequest as exc:
        log.warning("Telegram send_card failed: %s", exc)
        return None
