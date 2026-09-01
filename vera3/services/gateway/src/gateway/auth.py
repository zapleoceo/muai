"""Internal-service auth — shared by claude.py/query.py/events.py.

Was defined three times (near-identically) across those files, and a fourth
copy grew in brain-search. The comparison itself now lives in
`vera_shared.auth.internal_secret_ok` — the only place both services can
share, since they don't import each other. This module is just the
gateway-side HTTP shape.

Fail-closed: no secret configured = no access. The old copies were
fail-OPEN — if INTERNAL_SECRET was unset/empty the check silently passed
whatever the caller sent. docker-compose.yml requires the var
(`INTERNAL_SECRET:?must be set`), so it never bit in practice, but a
misconfigured non-compose deployment would go fully open with zero signal.
"""
from __future__ import annotations

from fastapi import HTTPException
from vera_shared.auth import internal_secret_ok

from gateway.config import get_settings


def check_internal_secret(provided: str | None) -> None:
    if not internal_secret_ok(provided, get_settings().internal_secret):
        raise HTTPException(401, "invalid internal secret")
