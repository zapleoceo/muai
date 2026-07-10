"""Internal-service auth — shared by claude.py/query.py/events.py.

Was defined three times (near-identically) across those files. All three
copies were also fail-OPEN: if INTERNAL_SECRET was unset/empty, the check
silently passed regardless of what the caller sent. docker-compose.yml
requires the var (`INTERNAL_SECRET:?must be set`), so this never bit in
practice — but a misconfigured non-compose deployment would go fully open
with zero signal. Fail-closed instead: no secret configured = no access.
"""
from __future__ import annotations

from fastapi import HTTPException

from gateway.config import get_settings


def check_internal_secret(provided: str | None) -> None:
    expected = get_settings().internal_secret
    if not expected or provided != expected:
        raise HTTPException(401, "invalid internal secret")
