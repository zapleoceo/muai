"""room_inbox_wait: исходящий длинный запрос агента вместо входящего вебхука.

Агенту не нужен публичный адрес: он сам держит запрос до `timeout_s`, сервер отвечает,
как только для него есть непрочитанное. Курсор не двигается — после обработки room_ack.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import Field
from vera_shared.room import messages

from vera_mcp import room_wake
from vera_mcp.room_common import MAX_BODY_CHARS, Agent, Consumer, Ident, Room
from vera_mcp.room_tools import room_inbox

#: Cloudflare рвёт ответ без данных на 100 с — держим запрос заметно меньше.
MAX_WAIT_S = 85
#: Подстраховка, если LISTEN-соединение молча умерло: перечитать БД хотя бы так часто.
RECHECK_S = 15.0


async def room_inbox_wait(
    ctx: Context, room: Room = "main", consumer: Consumer = messages.DEFAULT_CONSUMER,
    timeout_s: Annotated[int, Field(ge=1, le=MAX_WAIT_S)] = 80,
    after_id: Annotated[int | None, Field(ge=0)] = None,
    session: Ident | None = None, to: Agent | None = None,
    exclude_system: bool = True, limit: Annotated[int, Field(ge=1, le=200)] = 50,
    max_body_chars: Annotated[int, Field(ge=0, le=MAX_BODY_CHARS)] = 0,
) -> dict[str, Any]:
    """Как room_inbox, но ждёт до timeout_s (≤85), пока не появится непрочитанное; timed_out=true — ничего не пришло. after_id задаёт точку отсчёта вместо курсора. Курсор не двигает: после обработки room_ack. Long-poll for unread messages."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        seen = room_wake.version()
        result = await room_inbox(ctx, room=room, consumer=consumer, limit=limit,
                                  after_id=after_id, session=session, to=to,
                                  exclude_system=exclude_system, max_body_chars=max_body_chars)
        remaining = deadline - loop.time()
        if result["messages"] or remaining <= 0:
            return {**result, "timed_out": not result["messages"]}
        await room_wake.wait_since(seen, min(remaining, RECHECK_S))


WAIT_TOOLS = (room_inbox_wait,)
