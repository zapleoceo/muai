"""Runtime control flags — a tiny key/value table workers poll each loop.

Currently used for the backfill pause switch: the dashboard sets
`backfill_paused=1`, and brain-triage + media-worker skip claiming work
until it's cleared. Survives restarts (it's in Postgres), so a pause
holds across deploys.
"""
from __future__ import annotations

from sqlalchemy import text

from vera_shared.db.engine import get_session

BACKFILL_PAUSED = "backfill_paused"
BACKFILL_MAX_PER_HOUR = "backfill_max_per_hour"

# usage_log.workflow values that count as backfill LLM requests for the
# rate limit. Heavy calls only — embeds (voyage, cheap/batched) excluded.
_RATE_WORKFLOWS = ("triage", "media_vision", "media_voice")


async def get_control(key: str, default: str = "") -> str:
    async with get_session() as s:
        row = (await s.execute(
            text("SELECT value FROM app_control WHERE key = :k"), {"k": key}
        )).scalar_one_or_none()
    return row if row is not None else default


async def set_control(key: str, value: str) -> None:
    async with get_session() as s:
        await s.execute(text("""
            INSERT INTO app_control (key, value, updated_at)
            VALUES (:k, :v, now())
            ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = now()
        """), {"k": key, "v": value})


async def is_backfill_paused() -> bool:
    return (await get_control(BACKFILL_PAUSED, "0")) == "1"


async def set_backfill_paused(paused: bool) -> None:
    await set_control(BACKFILL_PAUSED, "1" if paused else "0")


async def get_backfill_max_per_hour() -> int:
    """Backfill request cap per hour. 0 = unlimited (no throttle)."""
    try:
        return max(0, int(await get_control(BACKFILL_MAX_PER_HOUR, "0")))
    except ValueError:
        return 0


async def set_backfill_max_per_hour(n: int) -> None:
    await set_control(BACKFILL_MAX_PER_HOUR, str(max(0, int(n))))


async def _requests_last_minute() -> int:
    """Heavy backfill requests (triage + media) in the trailing 60s, global
    across all workers/replicas — usage_log is the shared source of truth."""
    async with get_session() as s:
        return (await s.execute(text(
            "SELECT COUNT(*) FROM usage_log "
            "WHERE workflow = ANY(:wf) AND created_at > now() - interval '60 seconds'"
        ), {"wf": list(_RATE_WORKFLOWS)})).scalar() or 0


async def backfill_minute_allowance() -> int | None:
    """How many more backfill requests may run THIS minute, for smooth even
    pacing. None → unlimited (no cap set). 0 → rate reached, hold.

    Even-tempo: the hourly cap is spread to a per-minute budget; workers claim
    at most `allowance` items per cycle so the rate stays flat instead of
    bursting. Live events share the same budget (they also write workflow=
    'triage'), so the cap bounds total triage/media throughput, not just the
    historical backfill."""
    cap = await get_backfill_max_per_hour()
    if cap <= 0:
        return None
    per_min = max(1, round(cap / 60))
    used = await _requests_last_minute()
    return max(0, per_min - used)
