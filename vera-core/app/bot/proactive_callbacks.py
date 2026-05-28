"""Handle ✓/✗/mute on proactive DM cards."""
import logging

from aiogram import Bot, Router
from aiogram.types import CallbackQuery

from app.config import get_settings

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(lambda c: c.data and c.data.startswith("pro:"))
async def proactive_callback(callback: CallbackQuery, bot: Bot) -> None:
    settings = get_settings()
    if not callback.from_user or callback.from_user.id != settings.owner_telegram_id:
        await callback.answer("Только владельцу")
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("битый callback")
        return
    _, action, sig = parts[0], parts[1], ":".join(parts[2:])

    if action == "draft":
        # Pattern confirmed — pull Pattern + voice.apply_style, present draft
        from app.brain import patterns as P
        from app.brain.voice import apply_style
        pat = await P.get_pattern(sig)
        if not pat:
            await callback.answer("Pattern не найден")
            return
        template = (pat.get("action_label") or "")[:600]
        # rewrite в стиле Димы для этого чата (lazy: chat name as relationship)
        try:
            drafted = await apply_style(template, "default")
        except Exception:
            drafted = template
        await callback.answer("Готово")
        await bot.send_message(
            chat_id=settings.owner_telegram_id,
            text=f"📝 Черновик:\n\n<i>{drafted}</i>\n\n"
                  f"Скопируй и отправь — или скажи мне как поправить.",
            parse_mode="HTML",
        )
        # Bump confirmation — pass empty hints (proactive has no event_hints)
        try:
            ctx = P.context_key_for([])
            await P.upsert_confirmation(sig, ctx, action_label=template,
                                         tool=None, args=None)
        except Exception as exc:
            log.debug("proactive confirm shim: %s", exc)
    elif action == "skip":
        await callback.answer("ок, в этот раз пропускаю")
    elif action == "mute":
        from app.brain import patterns as P
        try:
            ctx = P.context_key_for([])
            await P.upsert_correction(sig, ctx, action_label="muted by user",
                                       tool=None, args=None)
        except Exception:
            pass
        await callback.answer("больше не предлагаю этот шаблон")
    else:
        await callback.answer("?")
