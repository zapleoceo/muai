"""Кэш дашборд-статистики — один тяжёлый проход по БД раз в TTL, а не на
каждую загрузку страницы и каждый poll /_progress.

Почему: таблица events ~3.9 ГБ (эмбеддинги лежат inline), поэтому любой
COUNT(*) = seq scan всей таблицы. Раньше home делал ~11 таких сканов, а
/_progress — ещё ~6 каждые 10 сек. На 2-vCPU сервере это и есть «страницы
грузятся долго». Здесь всё сводится к:
  • 1 GROUP BY source по events с FILTER-агрегатами → и разбивка по источникам,
    и все статусные счётчики (сумма по группам) за ОДИН проход;
  • 1 проход по usage_log (стоимость/темп);
результат кэшируется на TTL секунд и отдаётся и home, и /_progress.
Итого: ~2 скана раз в 30 сек вместо ~17 на каждый показ.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from vera_shared.db.engine import get_session

log = logging.getLogger(__name__)

# 60s > 30s poll interval → большинство poll'ов и все загрузки страниц между
# обновлениями попадают в тёплый кэш (мгновенно), тяжёлый скан платится ~раз/мин.
TTL_S = 60.0
_cache: dict[str, Any] = {"value": None, "mono": 0.0}
_stats_lock = asyncio.Lock()
# Ссылки на фоновые refresh-задачи — без них GC может собрать задачу
# на полпути и кэш молча перестанет обновляться.
_refresh_tasks: set[asyncio.Task] = set()


async def _bg_refresh(cache: dict, lock: asyncio.Lock, compute) -> None:
    if lock.locked():
        return  # уже кто-то обновляет — не плодим параллельные сканы
    async with lock:
        try:
            cache["value"] = await compute()
            cache["mono"] = time.monotonic()
        except Exception as e:  # noqa: BLE001
            log.warning("stats bg refresh failed: %s", e)


async def _serve_cached(cache: dict, lock: asyncio.Lock, compute, force: bool):
    """Stale-while-revalidate: отдаём кэш мгновенно даже если протух, а обновление
    делаем в фоне. Синхронно ждём только когда кэша ещё нет вообще (или force).
    Так тяжёлый скан почти никогда не блокирует запрос пользователя."""
    now_m = time.monotonic()
    val = cache["value"]
    fresh = val is not None and (now_m - cache["mono"]) < TTL_S

    if fresh and not force:
        return val
    if val is not None and not force:
        # Протухший есть — отдаём мгновенно, обновляемся в фоне.
        t = asyncio.create_task(_bg_refresh(cache, lock, compute))
        _refresh_tasks.add(t)
        t.add_done_callback(_refresh_tasks.discard)
        return val

    # Кэша нет (первый запрос после старта) или force — считаем синхронно,
    # под локом (double-check: пока ждали лок, кто-то мог уже посчитать).
    async with lock:
        if not force and cache["value"] is not None and \
                (time.monotonic() - cache["mono"]) < TTL_S:
            return cache["value"]
        cache["value"] = await compute()
        cache["mono"] = time.monotonic()
        return cache["value"]


async def get_stats(force: bool = False) -> dict[str, Any]:
    return await _serve_cached(_cache, _stats_lock, _compute_stats, force)


async def _compute_stats() -> dict[str, Any]:
    now = datetime.utcnow()
    h1 = now - timedelta(hours=1)
    h24 = now - timedelta(hours=24)
    today = now.date()
    month_ago = today - timedelta(days=30)

    async with get_session() as s:
        # ── ОДИН проход по events: разбивка по источникам + все счётчики ──
        rows = (await s.execute(text("""
            SELECT source,
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE triage_status='done')          AS done,
              COUNT(*) FILTER (WHERE triage_status='pending')       AS pending,
              COUNT(*) FILTER (WHERE triage_status='media_pending') AS media_pending,
              COUNT(*) FILTER (WHERE triage_status='error')         AS error,
              COUNT(*) FILTER (WHERE triage_status='dead')          AS dead,
              COUNT(*) FILTER (WHERE received_at >= :h1)  AS ingest_1h,
              COUNT(*) FILTER (WHERE received_at >= :h24) AS ingest_24h,
              MIN(occurred_at) AS earliest
            FROM events GROUP BY source
        """), {"h1": h1, "h24": h24})).mappings().all()

        # Эмбеддинги — в отдельной узкой таблице, счёт мгновенный (не скан events).
        with_emb = (await s.execute(
            text("SELECT COUNT(*) FROM event_embeddings"))).scalar() or 0

        # ── ОДИН проход по usage_log, и только по нужному окну ──
        # WHERE обязателен: самый широкий FILTER здесь — :month (30 дней), всё
        # остальное уже внутри него, поэтому результат тот же. Без него это
        # был полный скан ВСЕЙ таблицы, а она растёт на строку с каждого
        # LLM-вызова и не чистится (ретенция — scripts/prune_usage_log.sql).
        ul = (await s.execute(text("""
            SELECT
              COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= :today), 0) AS cost_today,
              COUNT(*)               FILTER (WHERE created_at >= :today)     AS calls_today,
              COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= :month), 0) AS cost_month,
              COUNT(*) FILTER (WHERE workflow='triage' AND created_at >= :h1)  AS triage_1h,
              COUNT(*) FILTER (WHERE workflow='triage' AND created_at >= :h24) AS triage_24h
            FROM usage_log
            WHERE created_at >= :month
        """), {"today": today, "month": month_ago, "h1": h1, "h24": h24})).mappings().one()

    # Свод по всем источникам (суммируем группы — без ещё одного скана)
    agg = dict.fromkeys(("total", "done", "pending", "media_pending", "error", "dead", "ingest_1h", "ingest_24h"), 0)
    per_source_total: list[tuple[str, int]] = []
    per_source_1h: list[tuple[str, int]] = []
    earliest: datetime | None = None
    for r in rows:
        for k in agg:
            agg[k] += r[k] or 0
        per_source_total.append((r["source"], r["total"]))
        if r["ingest_1h"]:
            per_source_1h.append((r["source"], r["ingest_1h"]))
        if r["earliest"] and (earliest is None or r["earliest"] < earliest):
            earliest = r["earliest"]

    per_source_total.sort(key=lambda x: x[1], reverse=True)
    per_source_1h.sort(key=lambda x: x[1], reverse=True)

    return {
        **agg,
        "with_emb": with_emb,
        "earliest": earliest,
        "backlog_total": agg["pending"] + agg["media_pending"] + agg["error"] + agg["dead"],
        "sources_top": per_source_total[:8],
        "per_source_1h": per_source_1h,
        "cost_today": float(ul["cost_today"]),
        "calls_today": ul["calls_today"],
        "cost_month": float(ul["cost_month"]),
        "triage_1h": ul["triage_1h"],
        "triage_24h": ul["triage_24h"],
        "computed_at": now,
    }


def cache_age_s() -> int:
    """Сколько секунд назад посчитан кэш (для пометки «обновлено N сек назад»)."""
    if _cache["value"] is None:
        return 0
    return int(time.monotonic() - _cache["mono"])


# ─── Страница источников ─────────────────────────────────────────────────────
# Раньше здесь считался один блоб на всю страницу с зашитыми ключами под
# telegram/instagram/gmail (tg_total, ig_1h, gmail_counts…): новый источник
# требовал правки и тут. Теперь два уровня, и оба не знают имён источников:
# обзор — один GROUP BY по всем сразу, подробности — по запросу, на источник.

_overview_cache: dict[str, Any] = {"value": None, "mono": 0.0}
_overview_lock = asyncio.Lock()

_detail_caches: dict[str, dict[str, Any]] = {}
_detail_locks: dict[str, asyncio.Lock] = {}


async def get_sources_overview(force: bool = False) -> dict[str, dict[str, Any]]:
    """`{source: {total, c1h, c24h, last}}` для списка источников. Один скан."""
    return await _serve_cached(_overview_cache, _overview_lock,
                               _compute_overview, force)


async def _compute_overview() -> dict[str, dict[str, Any]]:
    now = datetime.utcnow()
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT source, COUNT(*) AS total,
              COUNT(*) FILTER (WHERE received_at >= :h1)  AS c1h,
              COUNT(*) FILTER (WHERE received_at >= :h24) AS c24h,
              MAX(received_at) AS last
            FROM events GROUP BY source
        """), {"h1": now - timedelta(hours=1),
               "h24": now - timedelta(hours=24)})).mappings().all()
    return {r["source"]: dict(r) for r in rows}


async def get_source_detail(key: str, force: bool = False) -> list[dict[str, Any]]:
    """Разбивки одного источника. Кэш свой на каждый источник: страница
    подробностей открывается по требованию, а скан по 400 тыс. строк telegram
    незачем повторять на каждый показ."""
    cache = _detail_caches.setdefault(key, {"value": None, "mono": 0.0})
    lock = _detail_locks.setdefault(key, asyncio.Lock())

    async def compute():
        from dashboard.source_detail import blocks_for
        return await blocks_for(key)

    return await _serve_cached(cache, lock, compute, force)


def drop_detail_cache(key: str) -> None:
    """Сбросить кэш подробностей — после переподключения источника, чтобы
    страница не показывала «не подключено» ещё минуту."""
    _detail_caches.pop(key, None)
