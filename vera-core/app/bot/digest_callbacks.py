"""Digest feedback handler.

Reactions to the daily digest get translated into Pattern/Value signals
so Vera learns what kind of digest is useful:
  - 👍 norm  → no change, confirm
  - 🔍 more  → upsert a Value that prefers more detail
  - 🤫 quiet → bump VERA_CARD_MIN_SCORE up a notch (env-side effect)
  - 📢 loud  → bump VERA_CARD_MIN_SCORE down
"""
import logging
import os

from aiogram import Bot, Router
from aiogram.types import CallbackQuery

from app.config import get_settings

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(lambda c: c.data and c.data.startswith("dig:"))
async def digest_callback(callback: CallbackQuery, bot: Bot) -> None:
    settings = get_settings()
    if not callback.from_user or callback.from_user.id != settings.owner_telegram_id:
        await callback.answer("Только владельцу", show_alert=True)
        return

    choice = (callback.data or "dig:?").split(":", 1)[1]
    msg = "ok"

    if choice == "ok":
        msg = "Спасибо, продолжаю в том же духе"
        await _record_value("digest_style", "user confirmed", weight=1.0)
    elif choice == "more":
        msg = "Запомнила — в следующий раз будет детальнее"
        await _record_value("digest_detail", "user wants more depth", weight=2.0)
    elif choice == "quiet":
        cur = float(os.environ.get("VERA_CARD_MIN_SCORE", "5.0"))
        new = min(cur + 1.0, 10.0)
        os.environ["VERA_CARD_MIN_SCORE"] = str(new)
        msg = f"Стала тише: порог карточек {cur} → {new}"
        await _record_value("digest_quiet", f"raised threshold to {new}",
                              weight=2.0)
    elif choice == "loud":
        cur = float(os.environ.get("VERA_CARD_MIN_SCORE", "5.0"))
        new = max(cur - 1.0, 0.0)
        os.environ["VERA_CARD_MIN_SCORE"] = str(new)
        msg = f"Стала громче: порог карточек {cur} → {new}"
        await _record_value("digest_loud", f"lowered threshold to {new}",
                              weight=2.0)
    await callback.answer(msg)


async def _record_value(tag: str, statement: str, weight: float) -> None:
    try:
        from app.brain import identity as ID
        await ID.upsert_value(id=f"value_digest_{tag}",
                                statement=statement, weight=weight)
    except Exception as exc:
        log.debug("digest value record failed: %s", exc)
