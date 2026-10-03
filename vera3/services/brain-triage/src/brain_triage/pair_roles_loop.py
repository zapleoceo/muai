"""Фоновый вывод ролей пар по истории переписки (миграция 045).

Раз в PAIR_ROLES_INTERVAL_S берёт не больше PAIR_ROLES_BATCH самых нужных пар (пары владельца
первыми, `pair_roles_queue.pick_pairs`) и спрашивает модель через брокер. Первый сбой брокера
(в том числе открытый брейкер) прекращает проход: очередь не должна бить по лежащему брокеру.
По умолчанию ВЫКЛЮЧЕН (TRIAGE_PAIR_ROLES_ENABLED=0): первый проход тратит бюджет брокера на всю
очередь устоявшихся пар, включать после просмотра `scripts/infer_pair_roles.py --dry-run`.
Реплик может быть несколько: проход идёт под advisory-замком.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from vera_shared.db.engine import get_session
from vera_shared.graph.pair_roles import run_cycle

from brain_triage.config import (
    PAIR_ROLES_BATCH,
    PAIR_ROLES_ENABLED,
    PAIR_ROLES_INTERVAL_S,
    PAIR_ROLES_START_DELAY_S,
)

log = logging.getLogger(__name__)

PAIR_ROLES_LOCK_KEY = 7_340_045


@asynccontextmanager
async def _cycle_lock() -> AsyncIterator[bool]:
    async with get_session() as s:
        if getattr(getattr(s.bind, "dialect", None), "name", "") != "postgresql":
            yield True
            return
        got = (await s.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": PAIR_ROLES_LOCK_KEY})).scalar_one()
        try:
            yield bool(got)
        finally:
            if got:
                await s.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": PAIR_ROLES_LOCK_KEY})


async def run_pair_roles_cycle() -> int:
    """Один проход; → число пар, по которым звали модель (-1 — замок у другой реплики)."""
    async with _cycle_lock() as got:
        if not got:
            return -1
        results = await run_cycle(PAIR_ROLES_BATCH)
        found = sum(len(r.roles) for r in results)
        cost = sum(r.cost_usd for r in results)
        log.info("pair-roles: пар %d, ролей %d, cost_usd=%.6f, брокер=%s, формат=%d", len(results), found, cost,
                 "сбой" if any(r.failed for r in results) else "ок", sum(r.bad_format for r in results))
        return len(results)


async def pair_roles_loop() -> None:
    if not PAIR_ROLES_ENABLED:
        log.info("pair-roles: выключен (TRIAGE_PAIR_ROLES_ENABLED=0)")
    await asyncio.sleep(PAIR_ROLES_START_DELAY_S)
    while True:
        if PAIR_ROLES_ENABLED:
            try:
                await run_pair_roles_cycle()
            except Exception:
                log.exception("pair-roles: проход не удался (миграция 045 накачена?)")
        await asyncio.sleep(PAIR_ROLES_INTERVAL_S)
