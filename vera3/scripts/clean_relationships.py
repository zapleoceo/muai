#!/usr/bin/env python
"""Чистка связей графа: план → проверка глазами → применение → откат.

    python clean_relationships.py --plan plan.json                  # проход soft, БД не меняет
    python clean_relationships.py --phase verify --plan v.json --limit 50   # одиночные имена: модель
    python clean_relationships.py --plan plan.json --snapshot export.json
    python clean_relationships.py --apply plan.json --report rollback.json
    python clean_relationships.py --undo rollback.json

В проде (образ brain-triage, скрипты и отчёты — смонтированные тома):

    docker compose run --rm --no-deps -v /var/www/vera3/scripts:/scripts \\
        -v /var/lib/vera3-reports:/reports brain-triage \\
        python /scripts/clean_relationships.py --plan /reports/rel_plan.json

Проход soft не зовёт модель; проход verify (нужны БД и брокер, запускается на
сервере) проверяет каждую связь с одним словом вместо имени через
`vera_shared/graph/rel_verify.py` и гасит те, где модель не нашла прямого
утверждения с цитатой. Правила — `vera_shared/graph/rel_cleanup.py`, регламент — `docs/deploy-ops.md`.
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
from vera_shared.graph.rel_cleanup import (
    build_plan,
    plan_document,
    weak_name_candidates,
)
from vera_shared.graph.rel_cleanup_apply import PlanError, apply_plan, undo_report
from vera_shared.graph.rel_cleanup_snapshot import load_snapshot
from vera_shared.graph.rel_cleanup_verify import verify_plan

EXAMPLES = 10


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--plan", metavar="OUT.json", help="составить план (по умолчанию)")
    mode.add_argument("--apply", metavar="PLAN.json", help="применить план")
    mode.add_argument("--undo", metavar="ROLLBACK.json", help="откатить по отчёту")
    p.add_argument("--report", metavar="ROLLBACK.json",
                   help="куда писать отчёт для отката (обязателен с --apply)")
    p.add_argument("--phase", choices=("soft", "verify"), default="soft",
                   help="soft — правила без модели; verify — модель по одиночным именам")
    p.add_argument("--limit", type=int, metavar="N",
                   help="verify: пробная партия из первых N связей")
    p.add_argument("--concurrency", type=int, default=4, help="verify: параллельных вызовов")
    p.add_argument("--snapshot", metavar="EXPORT.json",
                   help="планировать по SELECT-выгрузке, без подключения к БД")
    return p


def _print_examples(doc: dict) -> None:
    seen: dict[str, int] = {}
    for a in doc["actions"]:
        if seen.get(a["rule"], 0) < EXAMPLES:
            seen[a["rule"]] = seen.get(a["rule"], 0) + 1
            print(f"  [{a['rule']}] #{a['rel_id']} {a['brief']}")


async def _plan(out: Path, args: argparse.Namespace) -> None:
    if args.snapshot and args.phase == "soft":
        data = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        source = f"snapshot:{Path(args.snapshot).name}"
    else:
        await init_engine()
        data = await load_snapshot(int(os.environ["OWNER_TELEGRAM_ID"]))
        source = "db"
    if args.phase == "soft":
        doc = plan_document(build_plan(data), source)
    else:
        cache = out.with_suffix(".verdicts.jsonl")
        actions, stats = await verify_plan(weak_name_candidates(data), cache,
                                           limit=args.limit, concurrency=args.concurrency)
        doc = {**plan_document(actions, source, "verify"), "stats": stats}
        print("вердикты:", json.dumps(stats))
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"план ({args.phase}): {doc['to_apply']} действий, по правилам "
          f"{json.dumps(doc['counts'])} → {out}")
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
        await _plan(Path(args.plan or "rel_plan.json"), args)
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
