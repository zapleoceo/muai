"""Индикатор сторожа в шапке `/tasks`: «сторож: N с назад», красный при простое или ошибке."""
from __future__ import annotations

from datetime import datetime

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import WatchdogStateRow
from vera_shared.room.watchdog import STATE_NAME

from dashboard.render import esc

STALE_AFTER_S = 3 * 60  # три интервала цикла


async def load_watchdog() -> WatchdogStateRow | None:
    async with get_session() as s:
        return await s.get(WatchdogStateRow, STATE_NAME)


def watchdog_badge(state: WatchdogStateRow | None, now: datetime) -> str:
    if state is None or state.last_run_at is None:
        return '<span class="pill warn">сторож: ещё не запускался</span>'
    age = max(int((now - state.last_run_at).total_seconds()), 0)
    bad = age > STALE_AFTER_S or bool(state.last_error)
    err = f" · {esc(state.last_error)}" if state.last_error else ""
    return f'<span class="pill {"warn" if bad else "ok"}">сторож: {age} с назад{err}</span>'
