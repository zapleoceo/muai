"""Отчёт о слиянии: достаточно, чтобы `unmerge` вернул граф как был.

Три вида следов, и этого хватает на любую таблицу:

* `moved`   — строка осталась, но поменяла внешний ключ (колонка и старое значение);
* `updated` — строка-победитель получила новые поля при склейке дубля (старые поля);
* `deleted` — строка удалена целиком (вся строка, чтобы вставить обратно с тем же id).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import DeclarativeBase

from vera_shared.graph.merge_codec import row_dict

REPORT_VERSION = 1


def _pk(row: DeclarativeBase) -> str:
    return row.__table__.primary_key.columns.keys()[0]


@dataclass
class MergeReport:
    keep_id: int
    drop_ids: list[int]
    reason: str
    merged_at: str
    keep_before: dict[str, Any] = field(default_factory=dict)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    moved: list[dict[str, Any]] = field(default_factory=list)
    updated: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[dict[str, Any]] = field(default_factory=list)
    version: int = REPORT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MergeReport:
        return cls(**data)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for kind, items in (("moved", self.moved), ("updated", self.updated),
                            ("deleted", self.deleted)):
            for item in items:
                key = f"{item['table']}_{kind}"
                out[key] = out.get(key, 0) + 1
        return out


class Recorder:
    """Пишет следы в отчёт; сами изменения делает вызывающий."""

    def __init__(self, report: MergeReport) -> None:
        self.report = report
        self._updated: set[tuple[str, Any]] = set()

    def moved(self, row: DeclarativeBase, column: str, old: Any) -> None:
        pk = _pk(row)
        self.report.moved.append({
            "table": row.__tablename__, "pk": pk, "pk_value": getattr(row, pk),
            "column": column, "old": old,
        })

    def deleted(self, row: DeclarativeBase) -> None:
        self.report.deleted.append(
            {"table": row.__tablename__, "row": row_dict(row)})

    def before_update(self, row: DeclarativeBase, fields: Iterable[str]) -> None:
        pk = _pk(row)
        marker = (row.__tablename__, getattr(row, pk))
        if marker in self._updated:
            return
        self._updated.add(marker)
        self.report.updated.append({
            "table": row.__tablename__, "pk": pk, "pk_value": getattr(row, pk),
            "before": {k: v for k, v in row_dict(row).items() if k in set(fields)},
        })
