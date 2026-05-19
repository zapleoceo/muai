from collections import deque
from typing import Deque

_MAX_TURNS = 6
_history: dict[int, Deque[tuple[str, str]]] = {}


def get_history(user_id: int | None) -> list[tuple[str, str]]:
    if user_id is None:
        return []
    return list(_history.get(user_id, ()))


def add_turn(user_id: int | None, user_msg: str, vera_reply: str) -> None:
    if user_id is None:
        return
    q = _history.setdefault(user_id, deque(maxlen=_MAX_TURNS))
    q.append((user_msg, vera_reply))


def format_history(user_id: int | None) -> str:
    history = get_history(user_id)
    if not history:
        return ""
    lines = []
    for u, v in history[-3:]:
        lines.append(f"User: {u}")
        lines.append(f"Vera: {v[:300]}")
    return "\n".join(lines)
