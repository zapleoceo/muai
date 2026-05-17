from aiogram import Router
from aiogram.types import CallbackQuery

router = Router()


@router.callback_query(lambda c: c.data and c.data.startswith("exec_approve:"))
async def on_approve(cb: CallbackQuery) -> None:
    item_id = int(cb.data.split(":")[1])
    await cb.answer("Отправляю...")
    from app.services.inbox_processor import send_via_executor
    ok = await send_via_executor(item_id, cb.bot)
    if not ok:
        await cb.message.edit_text(cb.message.text + "\n\n❌ Не удалось отправить")


@router.callback_query(lambda c: c.data and c.data.startswith("exec_ignore:"))
async def on_ignore(cb: CallbackQuery) -> None:
    item_id = int(cb.data.split(":")[1])
    await cb.answer("Проигнорировано")
    from app.services.inbox_processor import mark_ignored
    await mark_ignored(item_id, cb.bot)
