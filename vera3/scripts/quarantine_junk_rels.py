#!/usr/bin/env python
"""Прогнать уже записанные связи через ту же проверку, что стоит перед записью.

До 2026-09-03 rel-extract писал в граф без проверки типов, и mistral-small
наплодил мусора: «Olga Kryachko / Sintegrum works_at Olga Kryachko»,
«Ольга Крячко (JIRA) reports_to Vadim Kudryavtsev», «Link works_at OpenRouter,
Inc», тысяча с лишним «X works_at <чат, где X участник>». Правило одно —
`vera_shared.graph.rel_validate.relationship_reject_reason`, — поэтому то, что
скрипт пометит, живой rel-extract больше и не запишет.

Обратимо: не прошедшие получают `is_current = false`, строки остаются.
Список помеченных id с причинами пишется в `--report`; `--restore <файл>`
возвращает их обратно.

Запуск (transient-контейнер с env базы):
  docker compose run --rm --no-deps -v /var/www/vera3/scripts:/scripts \\
    brain-triage python /scripts/quarantine_junk_rels.py --dry-run
  … python /scripts/quarantine_junk_rels.py --report /scripts/junk_rels.json
  … python /scripts/quarantine_junk_rels.py --restore /scripts/junk_rels.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from vera_shared.db.engine import init_engine
from vera_shared.graph.rel_validate import relationship_reject_reason
from vera_shared.graph.repo_relationships import (
    list_current_relationships_page,
    set_relationships_current,
)

PAGE = 1000
EXAMPLES = 8


def _line(rel: dict[str, Any]) -> str:
    return (f"#{rel['id']} {rel['subject_name']} ({rel['subject_type']}) "
            f"-[{rel['predicate']}]-> {rel['object_name']} ({rel['object_type']})"
            f" | {(rel.get('fact') or '')[:80]}")


async def audit(page: int = PAGE) -> dict[str, Any]:
    """Проверить все текущие связи. Только чтение — пометка отдельно, после отчёта."""
    total = 0
    reasons: Counter[str] = Counter()
    by_predicate: Counter[str] = Counter()
    flagged: list[dict[str, Any]] = []
    examples: dict[str, list[str]] = {}
    kept: list[str] = []
    after = 0
    while True:
        rows = await list_current_relationships_page(after, page)
        if not rows:
            break
        after = rows[-1]["id"]
        for rel in rows:
            total += 1
            reason = relationship_reject_reason(
                subject_name=rel["subject_name"], subject_type=rel["subject_type"],
                predicate=rel["predicate"], object_name=rel["object_name"],
                object_type=rel["object_type"], confidence=float(rel["confidence"]),
            )
            if reason is None:
                if len(kept) < EXAMPLES * 2:
                    kept.append(_line(rel))
                continue
            reasons[reason] += 1
            by_predicate[rel["predicate"]] += 1
            flagged.append({"id": rel["id"], "reason": reason})
            bucket = examples.setdefault(reason, [])
            if len(bucket) < EXAMPLES:
                bucket.append(_line(rel))
    return {
        "total": total, "flagged": len(flagged),
        "kept": total - len(flagged), "reasons": dict(reasons),
        "by_predicate": dict(by_predicate), "examples": examples,
        "kept_examples": kept, "ids": flagged,
    }


async def mark(ids: list[int], *, current: bool) -> int:
    """Проставить `is_current` пачками по PAGE."""
    changed = 0
    for i in range(0, len(ids), PAGE):
        changed += await set_relationships_current(ids[i:i + PAGE], current=current)
    return changed


def report_ids(report_path: Path) -> list[int]:
    return [item["id"] for item in json.loads(report_path.read_text("utf-8"))["ids"]]


def _print_summary(report: dict[str, Any], *, applied: bool) -> None:
    mode = "ПОМЕЧЕНО" if applied else "DRY-RUN, будет помечено"
    print(f"связей текущих: {report['total']}; {mode}: {report['flagged']}; "
          f"остаётся: {report['kept']}")
    print("по причинам:", report["reasons"])
    print("по предикатам:", report["by_predicate"])
    for reason, lines in report["examples"].items():
        print(f"\n[{reason}]")
        for line in lines:
            print("  ", line)
    print("\n[остаётся]")
    for line in report["kept_examples"]:
        print("  ", line)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    parser.add_argument("--report", type=Path, help="куда записать id помеченных")
    parser.add_argument("--restore", type=Path, help="снять пометку по отчёту")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    await init_engine()
    if args.restore:
        print("возвращено:", await mark(report_ids(args.restore), current=True))
        return
    if not args.dry_run and not args.report:
        parser.error("без --dry-run нужен --report: иначе пометку не снять")
    report = await audit()
    # Отчёт — ДО пометки: если запись упадёт, база ещё не тронута.
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    if not args.dry_run:
        await mark([item["id"] for item in report["ids"]], current=False)
    _print_summary(report, applied=not args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
