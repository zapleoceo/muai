#!/usr/bin/env python
"""Переразбор старых писем Jira по diff-разметке (--dry-run по умолчанию).

    python reingest_jira_diff.py --dry-run [--limit N] [--since YYYY-MM-DD] [--event-id ID ...]
    python reingest_jira_diff.py --apply  [те же фильтры]     # бэкап, затем UPDATE
    python reingest_jira_diff.py --rollback /reports/reingest-jira-backup-<ts>.jsonl

Регламент и боевые команды — docs/deploy-ops.md. LLM не вызывается.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from ingestor_gmail.reingest_run import RunOptions, run
from ingestor_gmail.reingest_store import rollback
from vera_shared.db.engine import close_engine, init_engine


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="только отчёт (по умолчанию)")
    mode.add_argument("--apply", action="store_true", help="бэкап и UPDATE content_text")
    mode.add_argument("--rollback", metavar="BACKUP.jsonl", help="вернуть старый content_text")
    p.add_argument("--event-id", type=int, action="append", dest="event_ids")
    p.add_argument("--limit", type=int)
    p.add_argument("--since", type=datetime.fromisoformat, help="occurred_at >= YYYY-MM-DD")
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--reports", type=Path, default=Path("/reports"))
    return p


async def _main(args: argparse.Namespace) -> None:
    await init_engine()
    try:
        if args.rollback:
            print(json.dumps({"restored": await rollback(Path(args.rollback))}))
            return
        report = await run(RunOptions(
            apply=args.apply, event_ids=args.event_ids, since=args.since,
            limit=args.limit, batch=args.batch, reports=args.reports))
        print(json.dumps({k: report[k] for k in (
            "mode", "counts", "applied", "llm_calls", "timing_ms_per_message",
            "embeddings", "report_file", "backup_file")}, ensure_ascii=False, indent=2))
    finally:
        await close_engine()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_main(_parser().parse_args()))
