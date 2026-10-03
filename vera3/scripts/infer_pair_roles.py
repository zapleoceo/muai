#!/usr/bin/env python
"""Роли пар по истории переписки: ручной прогон (миграция 045, `vera_shared/graph/pair_roles.py`).

    # одна пара, БЕЗ записи: собрать пакет улик, спросить модель, напечатать результат
    python infer_pair_roles.py --pair 18,123 --dry-run
    # только пакет улик и оценка токенов, модель не зовётся, ничего не пишется
    python infer_pair_roles.py --pair 18,123 --show-pack
    # самые нужные пары очереди (пары владельца первыми), БЕЗ записи
    python infer_pair_roles.py --limit 5 --dry-run
    # записать (то же, что делает фоновый цикл)
    python infer_pair_roles.py --limit 20

В проде (нужны брокер и база — образ brain-triage):

    docker compose run --rm --no-deps -v /var/www/vera3/scripts:/scripts brain-triage \
        python /scripts/infer_pair_roles.py --pair <id1>,<id2> --dry-run

`--dry-run` вызывает модель (это стоит денег, цена пары печатается), но НИЧЕГО не пишет в базу.
`--force` игнорирует «пакет не изменился». Без `--dry-run` результат записывается и сразу виден
в карточке человека (с пометкой «выведено из переписки»).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import bindparam, text
from vera_shared.db.engine import close_engine, get_session, init_engine
from vera_shared.graph.pair_roles import (
    build_pair_evidence,
    estimate_tokens,
    infer_pair,
    run_cycle,
)
from vera_shared.graph.pair_roles_prompt import pack_payload, render_prompt
from vera_shared.graph.pair_roles_queue import pick_pairs
from vera_shared.graph.pair_roles_store import all_pair_stats, all_runs
from vera_shared.graph.pair_roles_types import A_TO_B, BOTH, PairInference
from vera_shared.graph.pair_stats import PairStats, ordered, stats_within
from vera_shared.links.context import owner_entity_id
from vera_shared.llm.broker_client import broker_enabled
from vera_shared.timeutil import utc_naive_now


def _readable(result: PairInference, names: dict[int, str]) -> dict:
    a, b = names.get(result.entity_a, "?"), names.get(result.entity_b, "?")

    def edge(direction: str, predicate: str) -> str:
        if direction == BOTH:
            return f"{a} <-[{predicate}]-> {b}"
        first, second = (a, b) if direction == A_TO_B else (b, a)
        return f"{first} -[{predicate}]-> {second}"

    return {"pair": [result.entity_a, result.entity_b, a, b], "skipped": result.skipped or None,
            "failed": result.failed, "bad_format": result.bad_format, "summary": result.summary, "model": result.model,
            "cost_usd": round(result.cost_usd, 6),
            "roles": [{"edge": edge(r.direction, r.predicate), "confidence": r.confidence,
                       "rationale": r.rationale, "quotes": list(r.quotes)} for r in result.roles]}


async def _names(ids: list[int]) -> dict[int, str]:
    async with get_session() as s:
        rows = (await s.execute(text("SELECT id, name FROM entities WHERE id IN :ids")
                                .bindparams(bindparam("ids", expanding=True)), {"ids": ids})).all()
    return dict(rows)


async def _show_pack(a: int, b: int) -> None:
    low, high = ordered(a, b)
    stats = (await stats_within([low, high])).get((low, high), PairStats())
    evidence = await build_pair_evidence(low, high, stats)
    print(json.dumps({"pair": [low, high], "messages": len(evidence.messages),
                      "estimated_input_tokens": estimate_tokens(render_prompt(evidence)),
                      "pack": pack_payload(evidence)}, ensure_ascii=False, indent=1, default=str))


async def main(args: argparse.Namespace) -> int:
    if not (args.pair or args.limit):
        print("нужен --pair a,b или --limit N", file=sys.stderr)
        return 2
    if not args.show_pack and not broker_enabled():
        print("BROKER_URL / BROKER_PROJECT_KEY не заданы — модель недоступна", file=sys.stderr)
        return 2
    await init_engine()
    try:
        if args.pair:
            pairs = [tuple(int(x) for x in p.split(",")) for p in args.pair]
            if args.show_pack:
                for a, b in pairs:
                    await _show_pack(a, b)
                return 0
            results = [await infer_pair(a, b, force=args.force, dry_run=args.dry_run) for a, b in pairs]
        elif args.dry_run:
            picked = pick_pairs(await all_pair_stats(), await all_runs(), await owner_entity_id(),
                                args.limit, utc_naive_now())
            results = [await infer_pair(a, b, force=args.force, dry_run=True) for a, b in picked]
        else:
            results = await run_cycle(args.limit)
        names = await _names(sorted({i for r in results for i in (r.entity_a, r.entity_b)}))
        for result in results:
            print(json.dumps(_readable(result, names), ensure_ascii=False, indent=1))
        total = sum(r.cost_usd for r in results)
        print(f"пар: {len(results)}, ролей: {sum(len(r.roles) for r in results)}, cost_usd: {total:.6f}"
              f"{' (dry-run, ничего не записано)' if args.dry_run else ''}")
    finally:
        await close_engine()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--pair", action="append", metavar="A,B", help="пара id сущностей (можно повторять)")
    p.add_argument("--limit", type=int, metavar="N", help="N самых нужных пар очереди")
    p.add_argument("--dry-run", action="store_true", help="спросить модель, но ничего не записывать")
    p.add_argument("--show-pack", action="store_true", help="только пакет улик; модель не зовётся")
    p.add_argument("--force", action="store_true", help="игнорировать «пакет не изменился»")
    sys.exit(asyncio.run(main(p.parse_args())))
