"""Per-source runtime counters that never claim a global zero outside observed coverage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dashboard.office_runtime_service import MAX_AGE, RuntimeSnapshot, _utc_naive


@dataclass(frozen=True)
class SourceSummary:
    source: str
    active: int
    coverage: str
    observed_at: datetime
    fresh: bool

    @property
    def text(self) -> str:
        if not self.fresh:
            return f"{self.source}: наблюдение устарело, текущий счёт неизвестен"
        if self.coverage == "complete":
            return f"{self.source}: {self.active} активных (покрытие полное)"
        if self.coverage == "unavailable":
            return f"{self.source}: источник недоступен, счёт неизвестен"
        floor = f"не менее {self.active} активных" if self.active else "активные не подтверждены"
        return f"{self.source}: {floor} (покрытие {self.coverage}, нуля не утверждаем)"


def source_summaries(snapshot: RuntimeSnapshot, now: datetime) -> list[SourceSummary]:
    names = {e.source for e in snapshot.entries} | {c[0] for c in snapshot.coverage}
    out = []
    for name in sorted(names):
        coverage, observed = snapshot.source_coverage(name)
        active = sum(e.source == name and e.state == "active" for e in snapshot.entries)
        fresh = _utc_naive(now) - observed <= MAX_AGE and observed <= _utc_naive(now)
        out.append(SourceSummary(name, active, coverage, observed, fresh))
    return out
