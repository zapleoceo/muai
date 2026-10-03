"""План слияния дублей → применение и откат.

План — JSON от `dupe_detect.build_plan`. Применяется по одному действию, каждое
в своей транзакции, а отчёт для отката переписывается после КАЖДОГО действия:
упавший на середине прогон оставляет валидный rollback того, что уже сделано.
Применять без пути отчёта нельзя — откат без отчёта невозможен.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import EntityAliasRow, EntityRow
from vera_shared.graph import entity_edit
from vera_shared.graph.dupe_actions import Action
from vera_shared.graph.merge import merge_entities
from vera_shared.graph.unmerge import unmerge

log = logging.getLogger(__name__)

PLAN_VERSION = 1


class PlanError(ValueError):
    """План или путь отчёта непригодны — ничего не изменено."""


def plan_document(actions: list[Action], source: str) -> dict[str, Any]:
    done = [a for a in actions if a["action"] != "skip"]
    counts: dict[str, dict[str, int]] = {}
    for a in actions:
        bucket = counts.setdefault(str(a["case"]), {})
        bucket[a["action"]] = bucket.get(a["action"], 0) + 1
    return {"version": PLAN_VERSION, "generated_at": datetime.now(UTC).isoformat(),
            "source": source, "to_apply": len(done), "counts": counts,
            "actions": actions}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=1))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


async def _stale(action: Action, touched: set[int]) -> str | None:
    """Почему действие нельзя применять: сущность исчезла или изменилась с
    момента составления плана (имя, тип, tg id, набор алиасов). Сущности,
    которые тронул сам прогон (`touched`: правки и keep прошлых слияний),
    не в счёт — у них алиасы и имя изменились по плану."""
    async with get_session() as s:
        for eid, brief in action["who"].items():
            row = await s.get(EntityRow, int(eid))
            if row is None:
                return f"сущности {eid} уже нет"
            if int(eid) in touched:
                continue
            aliases = {f"{a.source}:{a.identifier}" for a in (await s.execute(
                select(EntityAliasRow).where(EntityAliasRow.entity_id == int(eid))
            )).scalars()}
            tg_id = (row.attributes or {}).get("tg_id")
            if (row.name != brief["name"] or row.type != brief["type"]
                    or aliases != set(brief["aliases"])
                    or str(tg_id) != str(brief.get("tg_id"))):
                return f"сущность {eid} изменилась после плана"
    return None


async def _exec(action: Action, s: AsyncSession) -> dict[str, Any]:
    """Выполнить действие в чужой сессии: flush без commit."""
    if action["action"] == "merge":
        report = await merge_entities(action["keep"], action["drop"],
                                      f"case {action['case']}: {action['reason']}",
                                      session=s)
        return {"kind": "merge", "report": report.to_dict()}
    if action["action"] == "retype":
        edit = await entity_edit.retype_entity(action["entity"], action["new_type"], session=s)
    else:
        edit = await entity_edit.rename_entity(action["entity"], action["new_name"], session=s)
    return {"kind": "edit", "report": edit}


async def apply_plan(plan: dict[str, Any], report_path: str | Path | None, *,
                     cases: set[int] | None = None) -> dict[str, Any]:
    if not report_path:
        raise PlanError("--report обязателен: без отчёта слияние не откатить")
    path = Path(report_path)
    if path.exists():
        raise PlanError(f"{path} уже существует — не перезаписываю чужой rollback")
    if plan.get("version") != PLAN_VERSION:
        raise PlanError(f"неизвестная версия плана: {plan.get('version')!r}")
    out: dict[str, Any] = {"version": PLAN_VERSION, "entries": [], "skipped": []}
    _write_json(path, out)
    touched: set[int] = set()
    for action in plan["actions"]:
        if action["action"] == "skip" or (cases and action["case"] not in cases):
            continue
        if reason := await _stale(action, touched):
            log.warning("действие %s пропущено: %s", action["id"], reason)
            out["skipped"].append({"action_id": action["id"], "reason": reason})
            continue
        async with get_session() as s:
            entry = {"action_id": action["id"], "case": action["case"],
                     "undone": False, "status": "intent", **await _exec(action, s)}
            # Намерение — на диск ДО коммита: смерть между коммитом и записью
            # отчёта иначе оставила бы изменение без пути назад.
            out["entries"].append(entry)
            _write_json(path, out)
        entry["status"] = "done"
        _write_json(path, out)
        touched.add(entry["report"]["entity_id"] if entry["kind"] == "edit"
                    else entry["report"]["keep_id"])
    return out


async def _committed(entry: dict[str, Any]) -> bool:
    """Дошла ли транзакция записи-намерения до коммита — смотрим в БД."""
    async with get_session() as s:
        if entry["kind"] == "merge":
            gone = (await s.execute(select(EntityRow.id).where(
                EntityRow.id.in_(entry["report"]["drop_ids"])))).scalars().all()
            return not gone
        rep = entry["report"]
        row = await s.get(EntityRow, rep["entity_id"])
        return row is not None and getattr(row, rep["field"]) == rep["new"]


async def undo_report(report_path: str | Path) -> int:
    """Откатить в обратном порядке; уже откатанное пропускается, записи-намерения
    с незакоммиченной транзакцией — тоже. → сколько откатили."""
    path = Path(report_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    undone = 0
    for entry in reversed(data["entries"]):
        if entry["undone"]:
            continue
        if entry.get("status") == "intent" and not await _committed(entry):
            entry["undone"] = True
            entry["note"] = "транзакция не закоммичена — откатывать нечего"
            _write_json(path, data)
            continue
        if entry["kind"] == "merge":
            await unmerge(entry["report"])
        else:
            await entity_edit.revert_edit(entry["report"])
        entry["undone"] = True
        _write_json(path, data)
        undone += 1
    return undone
