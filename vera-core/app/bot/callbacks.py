import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.common.text import html_escape as _html_escape
from app.config import get_settings
from app.orchestrator.tool_router import call_tool, collect_tools, truncate_for_llm
from app.triage.dispatcher import record_user_decision, save_execution
from app.triage.pending import set_pending

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(lambda c: c.data and c.data.startswith("tri:"))
async def triage_callback(callback: CallbackQuery, bot: Bot) -> None:
    settings = get_settings()
    if not callback.from_user or callback.from_user.id != settings.owner_telegram_id:
        await callback.answer("Только владельцу", show_alert=True)
        log.warning("Rejected triage_callback from non-owner user_id=%s",
                    getattr(callback.from_user, "id", None))
        return
    # Accept callbacks from EITHER the legacy DM (vera_group_id) OR the
    # current forum chat (when topics-mode is on).
    from app.bot import preferences
    _prefs = await preferences.get_all()
    forum_chat = int(_prefs.get("forum_chat_id") or 0)
    chat_id = callback.message.chat.id if (callback.message and callback.message.chat) else None
    if chat_id is not None and chat_id != settings.vera_group_id and chat_id != forum_chat:
        await callback.answer("Не та переписка", show_alert=True)
        log.warning("Rejected callback from chat=%s (expected %s or %s)",
                    chat_id, settings.vera_group_id, forum_chat)
        return
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

    # Undo path — record explicit rejection of the auto-decision and skip
    # tool execution. Strong signal for retrieval next time.
    if choice == "undo":
        from app.triage.dispatcher import record_undo
        msg = await record_undo(event_id)
        await callback.answer("Откатила")
        try:
            if callback.message:
                base = callback.message.html_text or callback.message.text or ""
                await callback.message.edit_text(
                    base + f"\n\n<b>✋ Откачено:</b> {_html_escape(msg)}",
                    parse_mode="HTML", reply_markup=None,
                )
        except TelegramBadRequest as exc:
            log.warning("Failed to edit card on undo: %s", exc)
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
        await set_pending(user_id, event_id)
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(f"✍️ Что сделать с #{event_id}?\n\n"
                      f"<i>Просто напиши следующим сообщением — у тебя 5 минут. "
                      f"Примеры: «заархивируй», «ответь что я занят», «удали».</i>"),
                parse_mode="HTML",
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

    from app.bot import preferences
    prefs = await preferences.get_all()
    # Close or delete the forum topic this event lives in.
    thread_id = getattr(callback.message, "message_thread_id", None) if callback.message else None
    chat_id = callback.message.chat.id if (callback.message and callback.message.chat) else None
    if thread_id and chat_id:
        if prefs.get("delete_topic_on_decision"):
            try:
                await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
                # delete removes the whole topic — skip the card-edit below
                return
            except TelegramBadRequest as exc:
                log.warning("delete_forum_topic failed: %s — falling back to close", exc)
        if prefs.get("close_topic_on_decision"):
            try:
                await bot.close_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
            except TelegramBadRequest as exc:
                log.debug("close_forum_topic failed (probably already closed): %s", exc)
    try:
        if callback.message:
            if prefs.get("delete_card_after_decision"):
                await callback.message.delete()
                if prefs.get("execution_recap_in_dm") and suffix.strip():
                    await bot.send_message(
                        chat_id=callback.message.chat.id,
                        text=suffix.strip(), parse_mode="HTML",
                        message_thread_id=thread_id,
                    )
            else:
                base = callback.message.html_text or callback.message.text or ""
                await callback.message.edit_text(
                    base + suffix, parse_mode="HTML", reply_markup=None,
                )
    except TelegramBadRequest as exc:
        log.warning("Failed to update card: %s", exc)


