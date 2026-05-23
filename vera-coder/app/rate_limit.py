"""Process-local rate limit: max 1 task per hour."""
import time

from app.config import get_settings

_LAST: float = 0.0


def check_and_reserve() -> tuple[bool, float]:
    """Returns (allowed, seconds_until_next). Reserves slot on True."""
    global _LAST
    cfg = get_settings()
    interval = 3600.0 / max(1, cfg.rate_limit_per_hour)
    now = time.time()
    wait = (_LAST + interval) - now
    if wait > 0:
        return False, wait
    _LAST = now
    return True, 0.0
