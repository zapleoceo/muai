#!/usr/bin/env python
"""Прозвища людей с областью действия: список, добавить, предложить, решить, отчёт.

    python manage_nicknames.py list
    python manage_nicknames.py add --entity 123 --token XY --scope work
    python manage_nicknames.py suggest --entity 123          # инициалы «Имя Отчество» из переписки
    python manage_nicknames.py decide --id 5 --approve       # или --reject
    python manage_nicknames.py report --entity 123 --token XY --scope work   # сколько в области / вне (ничего не пишет)

Области: work (рабочие чаты + личка с сильными контактами), contacts (+ группы с двумя его
контактами), chats (только --chat telegram:<chat_id>), global. Токен регистрозависим.
В проде — через `docker compose run --rm --no-deps -v /var/www/vera3/scripts:/scripts brain-triage python /scripts/manage_nicknames.py …`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from vera_shared.db.engine import close_engine, init_engine
from vera_shared.graph.repo import get_entity
from vera_shared.links.nickname_suggest import scope_report, suggest_initials
from vera_shared.links.nicknames import (
    NicknameError,
    active_rules,
    add_nickname,
    decide_suggestion,
    pending_suggestions,
)
from vera_shared.links.scope import SCOPE_KINDS, WORK, NicknameRule


async def _run(args: argparse.Namespace) -> int:
    if args.cmd == "list":
        print(json.dumps({"active": [r.__dict__ for r in await active_rules()],
                          "suggested": await pending_suggestions()}, ensure_ascii=False, indent=1))
    elif args.cmd == "add":
        print("id:", await add_nickname(args.entity, args.token, scope_kind=args.scope,
                                        scope_ids=args.chat or [], case_sensitive=not args.ignore_case))
    elif args.cmd == "suggest":
        entity = await get_entity(args.entity)
        if entity is None:
            print("нет такой сущности", file=sys.stderr)
            return 2
        print("предложено:", await suggest_initials(entity.id, entity.name))
    elif args.cmd == "decide":
        print("решено:", await decide_suggestion(args.id, args.approve))
    else:
        rule = NicknameRule(args.entity, args.token, not args.ignore_case, args.scope,
                            tuple(args.chat or ()))
        print(json.dumps(await scope_report(rule), ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("add", "report"):
        c = sub.add_parser(name)
        c.add_argument("--entity", type=int, required=True)
        c.add_argument("--token", required=True)
        c.add_argument("--scope", choices=SCOPE_KINDS, default=WORK)
        c.add_argument("--chat", action="append", help="telegram:<chat_id> (область chats)")
        c.add_argument("--ignore-case", action="store_true")
    sub.add_parser("suggest").add_argument("--entity", type=int, required=True)
    d = sub.add_parser("decide")
    d.add_argument("--id", type=int, required=True)
    d.add_argument("--approve", action=argparse.BooleanOptionalAction, default=True)
    args = p.parse_args()

    async def go() -> int:
        await init_engine()
        try:
            return await _run(args)
        except NicknameError as e:
            print(f"отказ: {e}", file=sys.stderr)
            return 2
        finally:
            await close_engine()

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
