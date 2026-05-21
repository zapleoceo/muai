import asyncio
from app.graph import write as gw


async def main() -> None:
    # Persona-level instructions about self-loop avoidance.
    gw.write_instruction(169510539,
        "Сообщения от моего бота @Dimondra_Ai_Bot или в DM-чате с ним — "
        "это служебная переписка с тобой. Никогда не считай их новыми "
        "событиями, не создавай по ним карточки, не предлагай действий. "
        "Если событие пришло от такого источника — это баг, игнорируй.")
    gw.write_instruction(169510539,
        "Сообщения от @VerandamyBot в группе «Веранда сотрудники» — это "
        "автоматические уведомления о чеках/столах ресторана. Это служебный "
        "шум, мне для триажа не нужно. Игнорируй такие события.")
    # Give the background task a tick to actually fire.
    await asyncio.sleep(1)
    print("ok, two instructions enqueued for graph")


asyncio.run(main())
