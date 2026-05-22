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


def _topic_name(source: str, summary: str, event_id: int) -> str:
    icon = {"gmail": "📧", "telegram": "💬", "bank": "💰",
            "instagram": "📷", "facebook": "📘"}.get(source, "📥")
    short = (summary or "").strip().splitlines()[0] if summary else ""
    short = short[:48]
    base = f"{icon} {short or f'event #{event_id}'}"
    return base[:60]


async def _post_to_topic(bot, chat_id: int, topic_name: str,
                         text: str, kb) -> tuple[int | None, int | None]:
    """Create a forum topic and post inside it. Returns (msg_id, thread_id)."""
    try:
        topic = await bot.create_forum_topic(
            chat_id=chat_id, name=topic_name,
        )
        msg = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML",
            reply_markup=kb, message_thread_id=topic.message_thread_id,
        )
        return msg.message_id, topic.message_thread_id
    except TelegramBadRequest as exc:
        log.warning("topic post failed: %s", exc)
        return None, None


async def send_card(event_id: int, source: str, category: str,
                    proposal: TriageProposal, auto_exec: dict | None = None,
                    auto_note: str | None = None) -> dict | None:
    """Returns dict {msg_id, thread_id?, chat_id} or None on failure.
    Two layouts (manual/auto) × two transports (DM or forum topic)."""
    settings = get_settings()
    bot = get_bot()
    from app.bot import preferences
    prefs = await preferences.get_all()

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

    use_topics = bool(prefs.get("use_topics"))
    forum_chat = int(prefs.get("forum_chat_id") or 0)
    if use_topics and forum_chat:
        topic_name = _topic_name(source, proposal.summary, event_id)
        msg_id, thread_id = await _post_to_topic(bot, forum_chat, topic_name, text, kb)
        if msg_id is not None:
            return {"msg_id": msg_id, "thread_id": thread_id,
                    "chat_id": forum_chat}
        # Fall through to DM if topic post failed.

    try:
        msg = await bot.send_message(
            chat_id=settings.vera_group_id,
            text=text, parse_mode="HTML", reply_markup=kb,
        )
        return {"msg_id": msg.message_id, "thread_id": None,
                "chat_id": settings.vera_group_id}
    except TelegramBadRequest as exc:
        log.warning("Telegram send_card failed: %s", exc)
        return None
