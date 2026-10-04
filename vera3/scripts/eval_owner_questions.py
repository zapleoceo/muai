#!/usr/bin/env python
"""Вопросы владельца общими инструментами: сколько отвечается (только чтение, ничего не пишет).

    docker compose run --rm --no-deps -v /var/www/vera3/scripts:/scripts brain-triage \
        python /scripts/eval_owner_questions.py --lisa 123 --director 456 --oleg 789 --topic "CRM"

Двенадцать вопросов разной формы — `vera_shared/links/eval_questions.py`; каждый исполняется
цепочкой тех же функций, что стоят за MCP-инструментами (`search` оффлайн = фильтры + слова запроса
в тексте). Нужен индекс `event_entities` (миграции 042-044 и backfill). Печатает по каждому
вопросу: что было ДО связей (разбор контракта), непустой ли результат на ваших данных, итог.
Владелец определяется по `OWNER_TELEGRAM_ID`; люди и тема подставляются аргументами.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime

from sqlalchemy import bindparam, text
from vera_shared.db.engine import close_engine, get_session, init_engine
from vera_shared.links.context import owner_entity_id
from vera_shared.links.eval_questions import (
    CASES,
    after_level,
    render_question,
    run_case,
)

ROLES = ("lisa", "director", "oleg")


async def _names(ids: list[int]) -> dict[int, str]:
    async with get_session() as s:
        rows = (await s.execute(text("SELECT id, name FROM entities WHERE id IN :ids")
                                .bindparams(bindparam("ids", expanding=True)), {"ids": ids})).all()
    return dict(rows)


async def main(args: argparse.Namespace) -> int:
    await init_engine()
    try:
        owner = await owner_entity_id()
        if owner is None:
            print("не найден владелец (OWNER_TELEGRAM_ID)", file=sys.stderr)
            return 2
        period = {k: datetime.fromisoformat(v) for k, v in (("start", args.start), ("end", args.end)) if v}
        binds = {**period, "owner": owner, "topic": args.topic, **{r: getattr(args, r) for r in ROLES}}
        names = await _names([binds[r] for r in ROLES])
        label = {"topic": args.topic, **{r: names.get(binds[r], str(binds[r])) for r in ROLES}}
        before, after = Counter(), Counter()
        for case in CASES:
            result = await run_case(case, binds)
            level = after_level(result)
            before[case.before] += 1
            after[level] += 1
            steps = " -> ".join(f"{s['tool']}:{'есть' if s['non_empty'] else 'пусто'}" for s in result.steps)
            print(f"[{case.id}] {render_question(case, label)}\n   форма: {case.shape}; было: {case.before}"
                  f" ({case.before_note}); сейчас: {level}; шаги: {steps}")
        print(f"\nДО: {dict(before)}\nСЕЙЧАС: {dict(after)}")
    finally:
        await close_engine()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for role in ROLES:
        p.add_argument(f"--{role}", type=int, required=True, help=f"id сущности ({role})")
    p.add_argument("--start", help="начало периода вопросов «за период» (ISO; по умолчанию 2026-09-01)")
    p.add_argument("--end", help="конец периода (ISO; по умолчанию 2026-10-01)")
    p.add_argument("--topic", required=True, help="тема для вопросов про созвоны и упоминания")
    sys.exit(asyncio.run(main(p.parse_args())))
