"""Суточный дайджест открытых сроков — одно событие вместо события на карточку.

Снапшот доски по карточкам зафлудил бы events и сжёг бы бюджет триажа: правка
карточки прилетает и так через actions-фид. Здесь важно другое — что висит
незакрытым прямо сейчас.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from vera_shared.control import get_control, set_control

DIGEST_KEY = "trello_digest_date"
MAX_CARDS = 60


def _due_key(card: dict[str, Any]) -> str:
    return str(card.get("due") or "")


def _when(due: str) -> str:
    return f"{due[:10]} {due[11:16]}".strip()


def build_digest(
    per_board: list[tuple[str, list[dict[str, Any]]]], now: datetime,
) -> tuple[str, int, int] | None:
    """→ (текст, всего карточек, просроченных). None — если сроков нет вовсе."""
    stamp = now.isoformat()
    lines: list[str] = []
    printed = total = overdue = 0

    for board_name, cards in per_board:
        due_cards = sorted(
            (c for c in cards if c.get("due") and not c.get("dueComplete")),
            key=_due_key,
        )
        board_lines: list[str] = []
        for card in due_cards:
            total += 1
            due = str(card.get("due") or "")
            late = due < stamp
            overdue += 1 if late else 0
            if printed < MAX_CARDS:
                mark = " — ПРОСРОЧЕНО" if late else ""
                name = card.get("name") or "без названия"
                board_lines.append(f"— «{name}»: срок {_when(due)}{mark}")
                printed += 1
        if board_lines:
            lines.append(f"Доска «{board_name}»:")
            lines.extend(board_lines)
            lines.append("")

    if not total:
        return None
    if total > printed:
        lines.append(f"…и ещё {total - printed} карточек со сроками")

    header = (
        "Author: Я [self]\n"
        f"Trello: открытые задачи со сроками на {now:%Y-%m-%d}\n"
        f"Всего: {total}, просрочено: {overdue}\n"
        "---\n"
    )
    return header + "\n".join(lines).strip(), total, overdue


async def due_today(now: datetime) -> bool:
    """Дайджест за эту дату ещё не собирали."""
    return (await get_control(DIGEST_KEY, "")) != now.strftime("%Y-%m-%d")


async def mark_done(now: datetime) -> None:
    await set_control(DIGEST_KEY, now.strftime("%Y-%m-%d"))


def digest_event(text: str, total: int, overdue: int, now: datetime,
                 account: str) -> dict[str, Any]:
    return {
        "source": "trello",
        "source_event_id": f"digest:{now:%Y-%m-%d}",
        "account": account,
        "category": "digest",
        "content_text": text,
        "occurred_at": now,
        "entity_hints": [],
        "metadata_": {
            "author_role": "self",
            "author_label": "Я",
            "kind": "due_digest",
            "cards_total": total,
            "cards_overdue": overdue,
        },
    }
