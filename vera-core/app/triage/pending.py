"""In-memory map: user_id → event_id awaiting a free-text instruction."""
_pending: dict[int, int] = {}


def set_pending(user_id: int, event_id: int) -> None:
    _pending[user_id] = event_id


def pop_pending(user_id: int) -> int | None:
    return _pending.pop(user_id, None)


def has_pending(user_id: int) -> bool:
    return user_id in _pending
