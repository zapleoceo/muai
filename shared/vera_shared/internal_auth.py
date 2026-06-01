"""Shared HMAC check for the internal Vera service-to-service contract.

All vera-* containers use the same SESSION_SECRET-derived INTERNAL_SECRET
to talk to each other through the docker network. Routes that should only
be reachable from inside the cluster must call require_internal(header)
on every request.

Single source of truth — replaces four near-identical copies that lived
in app/internal/{agents,llm_proxy,coder}.py and app/events/routes.py.
"""
from __future__ import annotations

import hmac
import os

from fastapi import HTTPException


def _expected() -> str:
    s = os.environ.get("INTERNAL_SECRET")
    if not s:
        raise RuntimeError("INTERNAL_SECRET env var is not set")
    return s


def require_internal(secret_header: str | None) -> None:
    if not secret_header or not hmac.compare_digest(secret_header, _expected()):
        raise HTTPException(status_code=401, detail="invalid X-Internal-Secret")
