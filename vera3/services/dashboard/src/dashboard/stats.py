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
        asyncio.create_task(_bg_refresh(cache, lock, compute))
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

        # ── ОДИН проход по usage_log ──
        ul = (await s.execute(text("""
            SELECT
              COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= :today), 0) AS cost_today,
              COUNT(*)               FILTER (WHERE created_at >= :today)     AS calls_today,
              COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= :month), 0) AS cost_month,
              COUNT(*) FILTER (WHERE workflow='triage' AND created_at >= :h1)  AS triage_1h,
              COUNT(*) FILTER (WHERE workflow='triage' AND created_at >= :h24) AS triage_24h
            FROM usage_log
        """), {"today": today, "month": month_ago, "h1": h1, "h24": h24})).mappings().one()

    # Свод по всем источникам (суммируем группы — без ещё одного скана)
    agg = {k: 0 for k in ("total", "done", "pending", "media_pending", "error",
                          "dead", "ingest_1h", "ingest_24h")}
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


# ─── Кэш страницы /sources (тоже ~15 тяжёлых сканов) ─────────────────────────
_src_cache: dict[str, Any] = {"value": None, "mono": 0.0}
_src_lock = asyncio.Lock()


async def get_sources_stats(force: bool = False) -> dict[str, Any]:
    """Разбивки по telegram/instagram/gmail для /sources. stale-while-revalidate.

    Убран N+1 по gmail-аккаунтам (был COUNT в цикле по каждому ящику) — теперь
    один GROUP BY account. Остальные агрегаты сгруппированы по source за проход.
    """
    return await _serve_cached(_src_cache, _src_lock, _compute_sources_stats, force)


async def _compute_sources_stats() -> dict[str, Any]:
    now = datetime.utcnow()
    h1 = now - timedelta(hours=1)
    h24 = now - timedelta(hours=24)

    async with get_session() as s:
        # Один проход: total/1h/24h/last по каждому source (tg+ig и любые прочие)
        by_src = {r["source"]: r for r in (await s.execute(text("""
            SELECT source, COUNT(*) AS total,
              COUNT(*) FILTER (WHERE received_at >= :h1)  AS c1h,
              COUNT(*) FILTER (WHERE received_at >= :h24) AS c24h,
              MAX(received_at) AS last
            FROM events GROUP BY source
        """), {"h1": h1, "h24": h24})).mappings().all()}

        # Gmail per-account — ОДИН GROUP BY вместо цикла (был N+1)
        gmail_counts = dict((await s.execute(text(
            "SELECT account, COUNT(*) FROM events WHERE source='gmail' GROUP BY account"
        ))).all())

        tg_by_type = (await s.execute(text(
            "SELECT COALESCE(metadata->>'chat_type', category) AS t, COUNT(*) "
            "FROM events WHERE source='telegram' GROUP BY 1 ORDER BY 2 DESC"
        ))).all()
        tg_by_direction = (await s.execute(text(
            "SELECT COALESCE(metadata->>'direction','?'), COUNT(*) "
            "FROM events WHERE source='telegram' GROUP BY 1 ORDER BY 2 DESC"
        ))).all()
        tg_top_chats = (await s.execute(text(
            "SELECT COALESCE(metadata->>'chat_title','(unknown)'), "
            "COALESCE(metadata->>'chat_type','?'), COUNT(*) "
            "FROM events WHERE source='telegram' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20"
        ))).all()
        ig_by_direction = (await s.execute(text(
            "SELECT COALESCE(metadata->>'direction','?'), COUNT(*) "
            "FROM events WHERE source='instagram' GROUP BY 1 ORDER BY 2 DESC"
        ))).all()
        ig_top_threads = (await s.execute(text(
            "SELECT COALESCE(metadata->>'thread_title','(unknown)'), "
            "COALESCE((metadata->>'is_group')::text,'false'), COUNT(*) "
            "FROM events WHERE source='instagram' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20"
        ))).all()
        events_by_src = [(k, v["total"]) for k, v in
                         sorted(by_src.items(), key=lambda x: x[1]["total"], reverse=True)]

    def _src(name: str, field: str, default=0):
        r = by_src.get(name)
        return r[field] if r else default

    return {
        "gmail_counts": gmail_counts,
        "events_by_src": events_by_src,
        "tg_total": _src("telegram", "total"), "tg_1h": _src("telegram", "c1h"),
        "tg_24h": _src("telegram", "c24h"), "tg_last": _src("telegram", "last", None),
        "tg_by_type": list(tg_by_type),
        "tg_by_direction": list(tg_by_direction),
        "tg_top_chats": list(tg_top_chats),
        "ig_total": _src("instagram", "total"), "ig_1h": _src("instagram", "c1h"),
        "ig_24h": _src("instagram", "c24h"), "ig_last": _src("instagram", "last", None),
        "ig_by_direction": list(ig_by_direction),
        "ig_top_threads": list(ig_top_threads),
    }
