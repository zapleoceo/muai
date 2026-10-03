#!/usr/bin/env python
"""Точные дубли графа: план → проверка глазами → применение → откат.

    python merge_graph_duplicates.py --plan plan.json            # по умолчанию, БД не меняет
    python merge_graph_duplicates.py --plan plan.json --snapshot export.json
    python merge_graph_duplicates.py --apply plan.json --report rollback.json
    python merge_graph_duplicates.py --undo rollback.json

В проде (образ brain-triage, скрипты и отчёты — смонтированные тома):

    docker compose run --rm --no-deps -v /var/www/vera3/scripts:/scripts \\
        -v /var/lib/vera3-reports:/reports brain-triage \\
        python /scripts/merge_graph_duplicates.py --plan /reports/plan.json

Правила детектора и что НЕ сливается — `vera_shared/graph/dupe_detect.py`,
регламент — `docs/deploy-ops.md`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from vera_shared.db.engine import close_engine, init_engine
from vera_shared.graph.dupe_apply import (
    PlanError,
    apply_plan,
    plan_document,
    undo_report,
)
from vera_shared.graph.dupe_detect import build_plan
from vera_shared.graph.dupe_snapshot import load_snapshot, snapshot_from_dict

log = logging.getLogger("merge-graph-duplicates")


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
    p.add_argument("--case", type=int, action="append", choices=range(1, 6),
                   help="применять только этот случай (можно повторять)")
    return p


async def _plan(out: Path, snapshot: str | None) -> None:
    if snapshot:
        snap = snapshot_from_dict(json.loads(Path(snapshot).read_text(encoding="utf-8")))
        source = f"snapshot:{Path(snapshot).name}"
    else:
        await init_engine()
        snap = await load_snapshot()
        source = "db"
    doc = plan_document(build_plan(snap), source)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"план: {doc['to_apply']} действий к применению, счётчики по случаям "
          f"{json.dumps(doc['counts'])} → {out}")


async def _run(args: argparse.Namespace) -> int:
    if args.apply:
        if not args.report:
            print("отказ: --apply без --report — откатывать будет нечем", file=sys.stderr)
            return 2
        await init_engine()
        plan = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        result = await apply_plan(plan, args.report, cases=set(args.case or ()))
        print(f"применено {len(result['entries'])}, пропущено {len(result['skipped'])} "
              f"→ отчёт {args.report}")
    elif args.undo:
        await init_engine()
        print(f"откатано {await undo_report(args.undo)}")
    else:
        await _plan(Path(args.plan or "merge_plan.json"), args.snapshot)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
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
