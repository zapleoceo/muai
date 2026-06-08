"""Миграция данных Vera 2.0 → Vera 3.0.

Что мигрирует:
1. tokens — из tokens_decrypted.csv (бэкап) → новый tokens table с tier
2. events — из vera2_backup.db (SQLite) → новый events table в Postgres

Что НЕ мигрирует автоматически:
- Neo4j Graphiti — будет пересоздан с нуля (мы перестроим только важные через brain-graph)
- OAuth tokens Gmail — нужны те же что в env_server.txt (скопировать вручную)
- Telegram session — скопировать userbot.session в новый контейнер

Usage:
    python migrate_from_vera2.py \
        --backup /path/to/vera2-full-backup-FINAL/ \
        --target-db postgresql+asyncpg://vera:pwd@localhost/vera \
        --dry-run  # сначала с dry-run!
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Тип определяется по provider — соответствует PROVIDER_TIER в vera_shared
TIER_BY_PROVIDER = {
    "gemini": "free",       # default; per-key (paid demoniwwwe override below)
    "deepseek": "paid",
    "anthropic": "trial",
    "openrouter": "free",
    "cerebras": "free",
    "groq": "free",
    "voyage": "free",
    "manychat": "free",
    "nvidia": "free",
    "sambanova": "free",
    "mistral": "free",
}

# Конкретные overrides — некоторые ключи у тебя были paid даже если provider обычно free
PAID_OVERRIDES = {
    ("gemini", "demoniwwwe"),   # paid Gemini который дал $25 burn
}


async def migrate_tokens(csv_path: Path, *, dry_run: bool = True) -> int:
    """Импортирует токены из CSV (от backup) в новую tokens table."""
    if not csv_path.exists():
        log.error("CSV не найден: %s", csv_path)
        return 0

    from vera_shared.tokens import repository as token_repo

    count = 0
    skipped = 0
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            provider = row["provider"].lower()
            label = row["label"]
            token_raw = row["token"]

            if token_raw.startswith("FAIL:"):
                log.warning("Skip %s/%s — расшифровка не удалась", provider, label)
                skipped += 1
                continue

            # Определяем tier
            if (provider, label) in PAID_OVERRIDES:
                tier = "paid"
            else:
                tier = TIER_BY_PROVIDER.get(provider, "free")

            # Парсим capabilities (это JSON-like string в CSV)
            caps_raw = row.get("caps", "[]")
            try:
                caps = eval(caps_raw) if caps_raw else []  # noqa: S307 - controlled input
            except Exception:
                caps = []

            # Daily cap для paid токенов — берём $1 по умолчанию (можно увеличить через UI)
            daily_cap = None
            if tier == "paid":
                old_cap = row.get("cost_cap_usd", "").strip()
                if old_cap and old_cap != "None":
                    try:
                        daily_cap = float(old_cap)
                    except ValueError:
                        daily_cap = 1.0
                else:
                    daily_cap = 1.0  # консервативный default

            if dry_run:
                log.info(
                    "[DRY] Would import: %s/%s tier=%s caps=%s daily_cap=$%s",
                    provider, label, tier, caps, daily_cap,
                )
                count += 1
                continue

            try:
                await token_repo.upsert(
                    provider=provider,
                    label=label,
                    plaintext_token=token_raw,
                    tier=tier,
                    capabilities=caps,
                    daily_cost_cap_usd=daily_cap,
                    notes=f"migrated from Vera 2.0 on {datetime.utcnow().isoformat()}",
                )
                count += 1
                log.info("✓ %s/%s migrated (tier=%s)", provider, label, tier)
            except Exception as exc:
                log.error("✗ %s/%s failed: %s", provider, label, exc)
                skipped += 1

    log.info("Tokens migration: %d imported, %d skipped", count, skipped)
    return count


async def migrate_events(sqlite_path: Path, *, dry_run: bool = True, limit: int | None = None) -> int:
    """Копирует events из vera2 SQLite в новый Postgres events table.

    Skips дубликаты (по source+source_event_id уникальному ключу).
    """
    if not sqlite_path.exists():
        log.error("SQLite файл не найден: %s", sqlite_path)
        return 0

    from sqlalchemy import select
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row

    sql = (
        "SELECT source, source_event_id, account, category, content_text, "
        "content_extra, entity_hints, metadata, occurred_at, received_at, "
        "graphiti_episode_uuid, triage_status "
        "FROM events ORDER BY occurred_at"
    )
    if limit:
        sql += f" LIMIT {limit}"

    cursor = conn.execute(sql)

    imported = 0
    deduped = 0
    failed = 0
    batch_size = 500

    rows_batch: list[dict] = []
    for row in cursor:
        rows_batch.append(dict(row))
        if len(rows_batch) >= batch_size:
            r, d, f = await _import_batch(rows_batch, dry_run=dry_run)
            imported += r
            deduped += d
            failed += f
            rows_batch = []
            log.info("Progress: imported=%d deduped=%d failed=%d", imported, deduped, failed)

    if rows_batch:
        r, d, f = await _import_batch(rows_batch, dry_run=dry_run)
        imported += r
        deduped += d
        failed += f

    conn.close()
    log.info("Events migration: %d imported, %d dedup, %d failed", imported, deduped, failed)
    return imported


async def _import_batch(rows: list[dict], *, dry_run: bool) -> tuple[int, int, int]:
    """Batch insert with dedup."""
    import json as json_lib
    from sqlalchemy import select
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow

    if dry_run:
        return len(rows), 0, 0

    imported = 0
    deduped = 0
    failed = 0

    async with get_session() as s:
        # Check existing
        keys = [(r["source"], r["source_event_id"]) for r in rows]
        existing_query = select(EventRow.source, EventRow.source_event_id).where(
            EventRow.source.in_([k[0] for k in keys])
        )
        result = await s.execute(existing_query)
        existing_set = {(row.source, row.source_event_id) for row in result}

        for r in rows:
            key = (r["source"], r["source_event_id"])
            if key in existing_set:
                deduped += 1
                continue
            try:
                row = EventRow(
                    source=r["source"],
                    source_event_id=r["source_event_id"],
                    account=r.get("account"),
                    category=r.get("category", "generic"),
                    content_text=r.get("content_text") or "",
                    content_extra=_parse_json(r.get("content_extra")),
                    entity_hints=_parse_json(r.get("entity_hints")) or [],
                    metadata_=_parse_json(r.get("metadata")),
                    occurred_at=_parse_dt(r["occurred_at"]),
                    graphiti_episode_uuid=r.get("graphiti_episode_uuid"),
                    triage_status="pending",  # пересчитаем в Vera 3.0
                )
                s.add(row)
                imported += 1
            except Exception as exc:
                log.warning("Row failed: %s", exc)
                failed += 1

    return imported, deduped, failed


def _parse_json(raw):
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    import json
    try:
        return json.loads(raw)
    except Exception:
        return None


def _parse_dt(raw):
    if isinstance(raw, datetime):
        return raw
    if not raw:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(str(raw).split(".")[0])
    except Exception:
        return datetime.utcnow()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path, help="папка с распакованным vera2-full-backup-FINAL.tar.gz")
    parser.add_argument("--target-db", help="DATABASE_URL для Vera 3.0 Postgres (или из env)")
    parser.add_argument("--dry-run", action="store_true", help="не писать в БД, только показать что будет")
    parser.add_argument("--skip-tokens", action="store_true")
    parser.add_argument("--skip-events", action="store_true")
    parser.add_argument("--events-limit", type=int, help="ограничить кол-во events (для теста)")
    args = parser.parse_args()

    if args.target_db:
        import os
        os.environ["DATABASE_URL"] = args.target_db

    from vera_shared.db.engine import init_engine
    from vera_shared.db.models import Base

    log.info("Connecting to %s", args.target_db or "(from env)")
    engine = await init_engine()

    if not args.dry_run:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            log.info("Tables ensured")

    if not args.skip_tokens:
        log.info("=== Migrating tokens ===")
        await migrate_tokens(args.backup / "tokens_decrypted.csv", dry_run=args.dry_run)

    if not args.skip_events:
        log.info("=== Migrating events ===")
        await migrate_events(
            args.backup / "vera2_backup.db",
            dry_run=args.dry_run,
            limit=args.events_limit,
        )

    log.info("Migration complete")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("migrate")

if __name__ == "__main__":
    asyncio.run(main())
