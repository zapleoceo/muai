import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.config import get_settings
from app.orchestrator.tool_router import call_tool, collect_tools, truncate_for_llm
from app.triage.dispatcher import record_user_decision, save_execution
from app.triage.pending import set_pending

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

    if choice == "custom":
        owner_id = get_settings().owner_telegram_id
        user_id = callback.from_user.id if callback.from_user else owner_id
        set_pending(user_id, event_id)
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"✍️ Напиши что сделать с этим событием (#{event_id}). "
                     "Например: «заархивируй», «ответь что я занят», «удали».",
            )
        except TelegramBadRequest as exc:
            log.warning("Failed to prompt for custom instruction: %s", exc)

    suffix = f"\n\n<b>✓ Решено:</b> {_html_escape(label)}"

    tool = chosen.get("tool")
    if isinstance(tool, str) and tool:
        try:
            _, route = await collect_tools()
            result = await call_tool(route, tool, chosen.get("args") or {})
            ok = bool(result.get("ok"))
            preview = truncate_for_llm(result.get("result") or result.get("error"), 600)
            await save_execution(event_id, tool, chosen.get("args") or {}, result)
            mark = "✅" if ok else "⚠️"
            suffix += f"\n<b>{mark} {_html_escape(tool)}:</b> <code>{_html_escape(preview)}</code>"
        except Exception as exc:
            log.exception("Tool execution failed: %s", exc)
            suffix += f"\n<b>⚠️ Ошибка инструмента:</b> {_html_escape(str(exc)[:200])}"

    try:
        if callback.message:
            base = callback.message.html_text or callback.message.text or ""
            await callback.message.edit_text(base + suffix, parse_mode="HTML", reply_markup=None)
    except TelegramBadRequest as exc:
        log.warning("Failed to edit card: %s", exc)


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
