#!/usr/bin/env python
"""Чистка связей графа: план → проверка глазами → применение → откат.

    python clean_relationships.py --plan plan.json                  # по умолчанию, БД не меняет
    python clean_relationships.py --plan plan.json --snapshot export.json
    python clean_relationships.py --apply plan.json --report rollback.json
    python clean_relationships.py --undo rollback.json

В проде (образ brain-triage, скрипты и отчёты — смонтированные тома):

    docker compose run --rm --no-deps -v /var/www/vera3/scripts:/scripts \\
        -v /var/lib/vera3-reports:/reports brain-triage \\
        python /scripts/clean_relationships.py --plan /reports/rel_plan.json

Правила — `vera_shared/graph/rel_cleanup.py`, регламент — `docs/deploy-ops.md`.
Связи не удаляются: `is_current=false` либо приведение к канонической форме.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from vera_shared.db.engine import close_engine, init_engine
from vera_shared.graph.rel_cleanup import build_plan, plan_document
from vera_shared.graph.rel_cleanup_apply import PlanError, apply_plan, undo_report
from vera_shared.graph.rel_cleanup_snapshot import load_snapshot

EXAMPLES = 10


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--plan", metavar="OUT.json", help="составить план (по умолчанию)")
    mode.add_argument("--apply", metavar="PLAN.json", help="применить план")
    mode.add_argument("--undo", metavar="ROLLBACK.json", help="откатить по отчёту")
    p.add_argument("--report", metavar="ROLLBACK.json",
                   help="куда писать отчёт для отката (обязателен с --apply)")
    p.add_argument("--snapshot", metavar="EXPORT.json",
                   help="планировать по SELECT-выгрузке, без подключения к БД")
    return p


def _print_examples(doc: dict) -> None:
    seen: dict[str, int] = {}
    for a in doc["actions"]:
        if seen.get(a["rule"], 0) < EXAMPLES:
            seen[a["rule"]] = seen.get(a["rule"], 0) + 1
            print(f"  [{a['rule']}] #{a['rel_id']} {a['brief']}")


async def _plan(out: Path, snapshot: str | None) -> None:
    if snapshot:
        data = json.loads(Path(snapshot).read_text(encoding="utf-8"))
        source = f"snapshot:{Path(snapshot).name}"
    else:
        await init_engine()
        data = await load_snapshot(int(os.environ["OWNER_TELEGRAM_ID"]))
        source = "db"
    doc = plan_document(build_plan(data), source)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"план: {doc['to_apply']} действий, по правилам {json.dumps(doc['counts'])} → {out}")
    _print_examples(doc)


async def _run(args: argparse.Namespace) -> int:
    if args.apply:
        if not args.report:
            print("отказ: --apply без --report — откатывать будет нечем", file=sys.stderr)
            return 2
        await init_engine()
        plan = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        result = await apply_plan(plan, args.report)
        print(f"применено {len(result['entries'])}, пропущено {len(result['skipped'])} "
              f"→ отчёт {args.report}")
    elif args.undo:
        await init_engine()
        print(f"откатано {await undo_report(args.undo)}")
    else:
        await _plan(Path(args.plan or "rel_plan.json"), args.snapshot)
    return 0


def main() -> int:
    args = _parser().parse_args()

    async def go() -> int:
        try:
            return await _run(args)
        except PlanError as e:
            print(f"отказ: {e}", file=sys.stderr)
            return 2
        finally:
            await close_engine()

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
