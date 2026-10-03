"""Применение и откат плана чистки связей.

Действия идут пачками. Отчёт с намерениями пачки пишется на диск с fsync ДО
коммита (коммит — выход из сессии): смерть между коммитом и записью иначе
оставила бы изменение без пути назад. Откат возвращает только строки, чьё
состояние всё ещё равно «после» из отчёта, — чужие правки и непримёнившиеся
намерения не трогаются. Применять без пути отчёта нельзя.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import RelationshipRow
from vera_shared.graph.rel_cleanup import PLAN_VERSION

BATCH = 200


class PlanError(ValueError):
    """План или путь отчёта непригодны — ничего не изменено."""


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=1))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _state(row: RelationshipRow) -> dict[str, Any]:
    return {"subject_entity_id": row.subject_entity_id, "predicate": row.predicate,
            "object_entity_id": row.object_entity_id, "is_current": row.is_current}


def _set_state(row: RelationshipRow, state: dict[str, Any]) -> None:
    row.subject_entity_id = state["subject_entity_id"]
    row.predicate = state["predicate"]
    row.object_entity_id = state["object_entity_id"]
    row.is_current = state["is_current"]


async def _row(s: AsyncSession, rel_id: int) -> RelationshipRow | None:
    return (await s.execute(select(RelationshipRow).where(RelationshipRow.id == rel_id)
                            .with_for_update())).scalar_one_or_none()


async def apply_plan(plan: dict[str, Any], report_path: str | Path | None) -> dict[str, Any]:
    if not report_path:
        raise PlanError("--report обязателен: без отчёта чистку не откатить")
    path = Path(report_path)
    if path.exists():
        raise PlanError(f"{path} уже существует — не перезаписываю чужой rollback")
    if plan.get("version") != PLAN_VERSION:
        raise PlanError(f"неизвестная версия плана: {plan.get('version')!r}")
    todo = [a for a in plan["actions"] if a["action"] != "skip"]
    out: dict[str, Any] = {"version": PLAN_VERSION, "entries": [], "skipped": []}
    _write_json(path, out)
    for start in range(0, len(todo), BATCH):
        async with get_session() as s:
            batch: list[dict[str, Any]] = []
            for action in todo[start:start + BATCH]:
                row = await _row(s, action["rel_id"])
                if row is None or _state(row) != action["before"]:
                    out["skipped"].append({"action_id": action["id"],
                                           "reason": "строка изменилась после плана"})
                    continue
                try:
                    # SAVEPOINT: живой rel-extract мог успеть записать каноническую
                    # тройку — тогда падает только это действие, не вся пачка.
                    async with s.begin_nested():
                        _set_state(row, action["after"])
                        await s.flush()
                except IntegrityError:
                    out["skipped"].append({"action_id": action["id"],
                                           "reason": "конфликт уникальности: тройка занята"})
                    continue
                batch.append({"action_id": action["id"], "rel_id": row.id,
                              "rule": action["rule"], "before": action["before"],
                              "after": action["after"], "undone": False})
            out["entries"] += batch
            _write_json(path, out)
            await s.flush()
    return out


async def undo_report(report_path: str | Path) -> int:
    path = Path(report_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    undone = 0
    for start in range(0, len(data["entries"]), BATCH):
        async with get_session() as s:
            for entry in data["entries"][start:start + BATCH]:
                if entry["undone"]:
                    continue
                row = await _row(s, entry["rel_id"])
                if row is not None and _state(row) == entry["after"]:
                    try:
                        async with s.begin_nested():
                            _set_state(row, entry["before"])
                            await s.flush()
                        undone += 1
                    except IntegrityError:
                        entry["note"] = "конфликт уникальности: прежняя тройка занята"
                else:
                    entry["note"] = "строка изменилась после применения — не трогаю"
                entry["undone"] = True
            await s.flush()
        _write_json(path, data)
    return undone
