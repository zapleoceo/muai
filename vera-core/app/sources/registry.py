"""Source registry — discovery + lookup by name.

Each Source implementation registers itself here at import time
(`register(MySource())`). The jobs runner asks for a source by name
when claiming a backfill job; pollers iterate `all_sources()`.

Sources are in-memory singletons — they hold cursors and HTTP clients.
"""
from __future__ import annotations

import logging

from app.sources.base import Source

log = logging.getLogger(__name__)

_REGISTRY: dict[str, Source] = {}


def register(src: Source) -> None:
    if src.name in _REGISTRY:
        log.warning("source %r already registered — overwriting", src.name)
    _REGISTRY[src.name] = src


async def get_source(name: str) -> Source | None:
    await _ensure_loaded()
    return _REGISTRY.get(name)


async def all_sources() -> list[Source]:
    await _ensure_loaded()
    return list(_REGISTRY.values())


_loaded = False


async def _ensure_loaded() -> None:
    """Lazy discovery — import each source module so its top-level
    register() runs. Add new sources here as classes are written."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    # Imports are deferred so the registry module itself stays cheap.
    # Each module is responsible for instantiating + register()-ing.
    # (Gmail / Telegram modules will be added as they're refactored to
    # implement the Source contract.)
    try:
        from app.sources import gmail  # noqa: F401
    except ImportError:
        log.debug("source gmail not yet implemented — skipping")
    try:
        from app.sources import telegram  # noqa: F401
    except ImportError:
        log.debug("source telegram not yet implemented — skipping")
