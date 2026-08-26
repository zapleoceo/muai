#!/usr/bin/env python
"""Слить дубли по рабочему email. Сначала показать, потом делать.

Рабочий email глобально уникален, поэтому две person-сущности на один адрес —
детерминированный дубль, а не догадка. Подробности и почему они вообще
появились: `shared/vera_shared/graph/collisions.py`, `docs/identity.md`.

    python scripts/merge_email_dupes.py --dry-run   # что будет слито и почему
    python scripts/merge_email_dupes.py             # слить

Группы из трёх и более сущностей на один адрес НЕ сливаются: там уже не «пара»,
и разбирать их должен владелец на /entities/duplicates.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from vera_shared.db.engine import init_engine
from vera_shared.graph import dossiers
from vera_shared.graph.collisions import (
    find_email_collisions,
    merge_email_collision_pairs,
    pick_keeper,
)

log = logging.getLogger("merge-email")


def _show(title: str, d: dict) -> None:
    print(f"    {title}: #{d['entity_id']} {d['name']} "
          f"[{', '.join(d['channels']) or 'без каналов'}] "
          f"сообщений {d['msg_count']}, проект {d['dom_project'] or '—'}")
    for place, count in d["top_places"]:
        print(f"        где: {place} — {count}")
    for sample in d["samples"][:2]:
        print(f"        о чём: {sample[:110]}")


async def main(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    await init_engine()

    groups = await find_email_collisions()
    if not groups:
        print("дублей по email нет")
        return

    for group in groups:
        ids = [c["id"] for c in group["candidates"]]
        book = await dossiers.build(ids)
        print(f"\n{group['email']} — сущностей {group['size']}")
        if group["size"] != 2:
            print("    НЕ ПАРА — оставляю владельцу, разбирать на "
                  "/entities/duplicates")
            for eid in ids:
                _show("кандидат", book[eid])
            continue
        keeper, merged = pick_keeper(group["candidates"])
        _show("оставляю", book[keeper["id"]])
        _show("сливаю  ", book[merged["id"]])

    if dry_run:
        print("\n[dry-run] ничего не изменено")
        return

    done = await merge_email_collision_pairs()
    print(f"\nслито пар: {len(done)}")
    for d in done:
        moved = d.get("moved") or {}
        print(f"    {d['email']}: #{d['merged']} → #{d['keeper']} "
              f"(алиасов {moved.get('aliases_moved', '?')}, "
              f"связей {moved.get('relationships_moved', '?')})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args().dry_run))
