"""Проход `canonical` чистки: ТОЛЬКО привести форму связей к канону — для ВСЕХ текущих строк.

Проход `soft` пропускал связи с одиночным именем (их судит модель), а вместе с ними и
нормализацию: на проде остались `reports_to` и `child_of` в обратной форме, симметричные
строки с subject > object, дубли и пары «A над B» + «B над A». Здесь никого не судят по
смыслу и ничего не придумывают:

- `reports_to` / `child_of` и симметричные строки переписываются в каноническую
  тройку (`convert`), если она свободна;
- дубль канонической тройки гасится (`retire`) ТОЛЬКО если его событие-источник уже
  держит победитель: иначе строка — отдельная улика, и погашение уменьшило бы число
  независимых подтверждений роли (модель связи показывает иерархию от двух событий).
  Такие строки остаются (`skip`, `duplicate_kept_evidence`) — модель сворачивает формы
  сама;
- побеждает строка канонической формы (равные по форме — более весомая): уцелевшая
  обратная при конвертации упёрлась бы в погашенную каноническую, а
  `uq_relationships_spo` не смотрит на `is_current`;
- иерархия в обе стороны (противоречие) здесь НЕ гасится: одна сторона бывает верной
  правдой («штраф сотруднику» против разового обратного упоминания), угадать по числу
  строк нельзя. Её показывает модель связи — сторону с большей поддержкой.

Применение, отчёт и откат — прежние (`rel_cleanup_apply`).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from vera_shared.graph.rel_canon import SYMMETRIC, Triple, canonical_edge
from vera_shared.graph.rel_cleanup import (
    RULE_BLOCKED,
    RULE_INVERSE,
    RULE_SYMMETRIC,
    Action,
    Row,
    _brief,
    _convert,
    _rank,
    _triple,
    retire_action,
)

RULE_KEPT_EVIDENCE = "duplicate_kept_evidence"


def _skip(row: Row, rule: str) -> Action:
    return {"action": "skip", "rule": rule, "rel_id": row["id"], "keep_id": None,
            "brief": _brief(row)}


def canonical_plan(snapshot: dict[str, Any]) -> list[Action]:
    """План прохода `canonical` по срезу (`rel_cleanup_snapshot`), без базы."""
    rows: list[Row] = snapshot["relationships"]
    current = [r for r in rows if r["is_current"]]
    actions: list[Action] = []
    dead: set[int] = set()

    groups: dict[Triple, list[Row]] = defaultdict(list)
    for row in current:
        groups[canonical_edge(*_triple(row))].append(row)

    taken = {_triple(r) for r in rows}
    for canon, members in groups.items():
        # Побеждает строка КАНОНИЧЕСКОЙ формы (иначе уцелевшая обратная при конвертации
        # упёрлась бы в погашенную каноническую: uq_relationships_spo не смотрит is_current),
        # среди равных по форме — более весомая.
        alive = sorted((r for r in members if r["id"] not in dead),
                       key=lambda r: (_triple(r) == canon, _rank(r)), reverse=True)
        kept_events: set[Any] = set()
        for number, row in enumerate(alive):
            event = row.get("derived_from_event_id")
            if number and event is not None and event in kept_events:
                dead.add(row["id"])
                actions.append(retire_action(
                    row, RULE_SYMMETRIC if canon[1] in SYMMETRIC else RULE_INVERSE, alive[0]))
                continue
            kept_events.add(event)
            if _triple(row) == canon:
                continue
            if canon in taken:
                actions.append(_skip(row, RULE_KEPT_EVIDENCE if number else RULE_BLOCKED))
            else:
                taken.add(canon)
                actions.append(_convert(row))
    for number, action in enumerate(actions, 1):
        action["id"] = number
    return actions
