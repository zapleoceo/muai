#!/usr/bin/env python
"""Связи, привязанные к тёзке вне круга разговора: план → глазами → применение → откат.

    python clean_namesakes.py --plan plan.json --entity 1054        # только его связи, БД не меняет
    python clean_namesakes.py --plan plan.json                      # все одиночные имена графа
    python clean_namesakes.py --apply plan.json --report rollback.json
    python clean_namesakes.py --undo rollback.json

В проде (образ brain-triage, как `clean_relationships.py`):

    docker compose run --rm --no-deps -v /var/www/vera3/scripts:/scripts \
        -v /var/lib/vera3-reports:/reports brain-triage \
        python /scripts/clean_namesakes.py --plan /reports/namesake_plan.json --entity 1054

Правило — `vera_shared/links/namesake_plan.py`: конец-одиночное имя вне круга события-источника
переставляется на единственного подходящего человека круга (`namesake_repoint`) либо
гасится (`namesake_retire`). Применение и откат — те же, что у `clean_relationships.py`
(`rel_cleanup_apply`): отчёт на диск ДО коммита, откат возвращает только неизменившиеся
строки. Связи не удаляются.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from vera_shared.db.engine import close_engine, init_engine
from vera_shared.graph.rel_cleanup_apply import PlanError, apply_plan, undo_report
from vera_shared.links.namesake_plan import (
    build_namesake_plan,
    load_rows,
    namesake_document,
)

EXAMPLES = 15


async def _plan(out: Path, entities: list[int] | None) -> None:
    await init_engine()
    rows = await load_rows(entities)
    actions = await build_namesake_plan(rows)
    scope = f"entities:{','.join(map(str, entities))}" if entities else "all"
    doc = namesake_document(actions, scope)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"просмотрено связей: {len(rows)}; действий {doc['to_apply']}: {json.dumps(doc['by_rule'])} → {out}")
    for action in actions[:EXAMPLES]:
        print(f"  [{action['rule']}] #{action['rel_id']} {action['brief']}")


async def _run(args: argparse.Namespace) -> int:
    if args.apply:
        if not args.report:
            print("отказ: --apply без --report — откатывать будет нечем", file=sys.stderr)
            return 2
        await init_engine()
        plan = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        result = await apply_plan(plan, args.report)
        print(f"применено {len(result['entries'])}, пропущено {len(result['skipped'])} → отчёт {args.report}")
    elif args.undo:
        await init_engine()
        print(f"откатано {await undo_report(args.undo)}")
    else:
        await _plan(Path(args.plan or "namesake_plan.json"), args.entity)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--plan", metavar="OUT.json", help="составить план (по умолчанию)")
    mode.add_argument("--apply", metavar="PLAN.json", help="применить план")
    mode.add_argument("--undo", metavar="ROLLBACK.json", help="откатить по отчёту")
    p.add_argument("--report", metavar="ROLLBACK.json", help="куда писать отчёт (обязателен с --apply)")
    p.add_argument("--entity", type=int, action="append", help="только связи этой сущности (можно повторять)")
    args = p.parse_args()

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
