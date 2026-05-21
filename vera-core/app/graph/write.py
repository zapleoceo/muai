"""Higher-level Graphiti writers — decisions, instructions, executions.
Fire-and-forget. Failures are logged, never raised."""
import asyncio
import logging
from datetime import datetime

log = logging.getLogger(__name__)


async def _add(name: str, body: str, ref_time: datetime | None = None,
               description: str = "vera-feedback") -> None:
    try:
        from graphiti_core.nodes import EpisodeType
        from app.graph.client import get_graphiti
        client = await get_graphiti()
        await asyncio.wait_for(
            client.add_episode(
                name=name,
                episode_body=body,
                source=EpisodeType.text,
                source_description=description,
                reference_time=ref_time or datetime.utcnow(),
                group_id="vera",
            ),
            timeout=45.0,
        )
        log.info("Graph episode written: %s", name)
    except Exception as exc:
        log.warning("Graph episode %s failed: %s", name, exc)


def write_decision(event_id: int, source: str, sender: str | None,
                   chosen_label: str, chosen_tool: str | None,
                   summary: str | None) -> None:
    body_parts = [
        f"Дима принял решение по событию #{event_id} ({source}):",
        f"Выбрал: «{chosen_label}»",
    ]
    if chosen_tool:
        body_parts.append(f"Инструмент: {chosen_tool}")
    if sender:
        body_parts.append(f"Отправитель: {sender}")
    if summary:
        body_parts.append(f"Контекст события: {summary}")
    from app.common.bg import spawn as _spawn
    _spawn(_add(
        name=f"decision/{event_id}",
        body="\n".join(body_parts),
        description="user decision",
    ))


def write_execution(event_id: int, tool: str, ok: bool,
                    args: dict | None, sender: str | None) -> None:
    status = "успешно" if ok else "с ошибкой"
    body_parts = [
        f"Vera выполнила инструмент {tool} по событию #{event_id} {status}.",
    ]
    if args:
        body_parts.append(f"Аргументы: {', '.join(f'{k}={v}' for k,v in args.items())[:300]}")
    if sender:
        body_parts.append(f"Отправитель: {sender}")
    from app.common.bg import spawn as _spawn
    _spawn(_add(
        name=f"execution/{event_id}/{tool}",
        body="\n".join(body_parts),
        description="tool execution",
    ))


def write_instruction(user_id: int, text: str) -> None:
    """Persist a free-text instruction Dima gave to the bot in DM. These
    are long-lived preference statements ('игнорируй verandamybot')."""
    body = f"Дима написал инструкцию боту: «{text}»"
    from app.common.bg import spawn as _spawn
    _spawn(_add(
        name=f"instruction/{user_id}/{int(datetime.utcnow().timestamp())}",
        body=body,
        description="dima instruction",
    ))


def write_rejection(event_id: int, source: str, sender: str | None,
                    summary: str | None) -> None:
    """Explicit ignore/skip — important signal for future similar events."""
    body_parts = [
        f"Дима ИГНОРИРОВАЛ событие #{event_id} ({source}).",
    ]
    if sender:
        body_parts.append(f"Отправитель: {sender} — такие события ему не нужны.")
    if summary:
        body_parts.append(f"Что было: {summary}")
    from app.common.bg import spawn as _spawn
    _spawn(_add(
        name=f"rejection/{event_id}",
        body="\n".join(body_parts),
        description="user rejection",
    ))
