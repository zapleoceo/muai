import logging

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import inspect, select, text

from vera_shared.db.models import Base, Token
from vera_shared.db.engine import get_session

log = logging.getLogger(__name__)

_DEFAULT_CAPS: dict[str, list[str]] = {
    "gemini": ["chat:fast", "prefilter"],
    "deepseek": ["chat:fast", "chat:smart", "chat:code"],
    "voyage": ["embed"],
    "anthropic": ["chat:smart", "chat:code"],
    "openrouter": ["chat:fast", "chat:smart", "prefilter"],
}


async def run_migrations(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.run_sync(_evolve_columns)
        await conn.run_sync(_ensure_indexes)

    await _seed_default_caps()


def _ensure_indexes(sync_conn) -> None:
    """Idempotent index/constraint creation.
    UNIQUE on (source, source_event_id) closes the dedup race in save_event.
    """
    try:
        sync_conn.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS '
            'ux_events_source_eid '
            'ON events(source, source_event_id) '
            'WHERE source_event_id IS NOT NULL'
        ))
        log.info("schema-evolve: ux_events_source_eid index ensured")
    except Exception as exc:
        # If duplicates already exist, the index creation will fail.
        # Log and continue — operator can dedup manually.
        log.warning("schema-evolve: ux_events_source_eid skipped: %s", exc)


def _evolve_columns(sync_conn) -> None:
    """SQLite create_all() never adds columns to existing tables. Walk
    each model declared on Base.metadata, diff against the live schema,
    and ALTER TABLE ADD COLUMN whatever is missing. Idempotent.

    Limitations: only adds columns. Doesn't drop, rename, or change
    types. For destructive changes, write a one-shot script."""
    insp = inspect(sync_conn)
    existing_tables = set(insp.get_table_names())
    added = 0
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table_name)}
        for col in table.columns:
            if col.name in existing_cols:
                continue
            try:
                col_def = _sqlite_col_def(col)
                sync_conn.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN {col_def}')
                )
                log.info("schema-evolve: %s.%s added (%s)",
                         table_name, col.name, col.type)
                added += 1
            except Exception as exc:
                log.warning("schema-evolve: %s.%s failed: %s",
                            table_name, col.name, exc)
    if added:
        log.info("schema-evolve: %d new columns applied", added)


def _sqlite_col_def(col) -> str:
    """Render a SQLAlchemy Column as the right side of ADD COLUMN.
    SQLite requires literal defaults — we coerce to NULL otherwise."""
    parts = [f'"{col.name}"', col.type.compile(dialect=None)
             if hasattr(col.type, "compile") else str(col.type)]
    # SQLite ALTER TABLE ADD COLUMN cannot use non-constant defaults.
    if col.default is not None and getattr(col.default, "is_scalar", False):
        v = col.default.arg
        if isinstance(v, bool):
            parts.append(f"DEFAULT {1 if v else 0}")
        elif isinstance(v, (int, float)):
            parts.append(f"DEFAULT {v}")
        elif isinstance(v, str):
            parts.append(f"DEFAULT '{v.replace(chr(39), chr(39)*2)}'")
    if not col.nullable and "DEFAULT" not in " ".join(parts):
        # ADD COLUMN NOT NULL without a default is illegal in SQLite.
        # Fall back to nullable; new columns of existing rows will be NULL.
        pass
    return " ".join(parts)


async def _seed_default_caps() -> None:
    async with get_session() as session:
        result = await session.execute(select(Token).limit(1))
        if result.scalar_one_or_none() is not None:
            return

        # No tokens yet — nothing to seed. Actual token values come from env/UI.
        # We only store capability metadata, not keys, so no seeding needed here.
