"""The single counting rule for runtime streams, shared by the global, project and source views.

A source counts only when its own observation is fresh and its coverage is partial or complete.
A zero is claimed only when every source in scope is complete and fresh and no entry is unknown.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from dashboard.office_runtime_service import (
    MAX_AGE,
    UNKNOWN_SOURCE,
    RuntimeEntry,
    RuntimeSnapshot,
    utc_naive,
)

COUNTED_COVERAGE = ("partial", "complete")


@dataclass(frozen=True)
class RuntimeCount:
    confirmed: int
    unproven: int
    proven_zero: bool

    @property
    def text(self) -> str:
        if self.confirmed:
            tail = f"; не подтверждено: {self.unproven}" if self.unproven else ""
            return f"{self.confirmed} подтверждено{tail}"
        return "0 подтверждено" if self.proven_zero else "не подтверждено"


@dataclass(frozen=True)
class SourceSummary:
    source: str
    count: RuntimeCount
    coverage: str
    observed_at: datetime | None
    fresh: bool

    @property
    def text(self) -> str:
        if not self.fresh:
            why = "не указано время наблюдения" if self.observed_at is None else "наблюдение устарело"
            return f"{self.source}: {why}, текущий счёт неизвестен"
        if self.coverage == "unavailable":
            return f"{self.source}: источник недоступен, счёт неизвестен"
        if self.coverage == "unknown":
            return f"{self.source}: без заявленного покрытия, только историческое наблюдение"
        c = self.count
        if c.proven_zero:
            return f"{self.source}: 0 активных (покрытие полное)"
        if c.confirmed:
            return f"{self.source}: не менее {c.confirmed} активных (покрытие {self.coverage})"
        return f"{self.source}: активные не подтверждены (покрытие {self.coverage})"


def _source_state(snapshot: RuntimeSnapshot, name: str, now: datetime) -> tuple[str, datetime, bool]:
    coverage, observed = snapshot.source_coverage(name)
    # no explicit observed_at means unknown freshness, never inherited from checked_at
    fresh = observed is not None and timedelta(0) <= utc_naive(now) - observed <= MAX_AGE
    return coverage, observed, fresh


def runtime_count(snapshot: RuntimeSnapshot, now: datetime, *, project: str | None = None,
                  source: str | None = None) -> RuntimeCount:
    entries: list[RuntimeEntry] = [
        e for e in snapshot.entries
        if (project is None or e.project.casefold() == project.casefold())
        and (source is None or e.source == source)]
    names = {e.source for e in entries}
    if source is None:
        names |= {c[0] for c in snapshot.coverage}
    else:
        names.add(source)
    counted: set[str] = set()
    all_complete = bool(names)
    for name in names:
        coverage, _, fresh = _source_state(snapshot, name, now)
        if fresh and coverage in COUNTED_COVERAGE and name != UNKNOWN_SOURCE:
            counted.add(name)
        all_complete = all_complete and fresh and coverage == "complete" and (
            name != UNKNOWN_SOURCE)
    confirmed = sum(e.state == "active" and e.source in counted for e in entries)
    unproven = sum(e.source not in counted or e.state == "unknown" for e in entries)
    return RuntimeCount(confirmed, unproven, confirmed == 0 and unproven == 0 and all_complete)


def source_summaries(snapshot: RuntimeSnapshot, now: datetime) -> list[SourceSummary]:
    names = {e.source for e in snapshot.entries} | {c[0] for c in snapshot.coverage}
    out = []
    for name in sorted(names):
        coverage, observed, fresh = _source_state(snapshot, name, now)
        out.append(SourceSummary(name, runtime_count(snapshot, now, source=name),
                                 coverage, observed, fresh))
    return out
