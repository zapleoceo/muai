"""Pure derivations for Office card details: artifact, executor, dependencies, hourly delta."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from vera_shared.redact_view import redact_view

from dashboard.office_status import StatusInfo

TEXT_LIMIT = 280
REF_LIMIT = 10
TRANSITION_KINDS = ("claimed", "paused", "resumed", "review", "blocked", "unblocked",
                    "released", "waiting", "handoff_accept", "ack_answer", "cancelled")
TRANSITION_LABELS = {
    "claimed": "взята", "paused": "пауза", "resumed": "возобновлена", "review": "на ревью",
    "blocked": "блокер", "unblocked": "блокер снят", "released": "аренда снята",
    "waiting": "ожидание", "handoff_accept": "передача принята", "ack_answer": "ответ принят",
    "cancelled": "отменена"}
UNCHANGED = "без изменений"
EXECUTOR_LABELS = {"lease": "держатель аренды", "responsible": "ответственный",
                   "owner": "владелец"}


@dataclass(frozen=True)
class RefLink:
    kind: str
    ref: str
    url: str | None


@dataclass(frozen=True)
class Artifact:
    at: datetime
    kind: str
    text: str
    refs: tuple[RefLink, ...]


@dataclass(frozen=True)
class Executor:
    name: str
    label: str


@dataclass(frozen=True)
class DependencyState:
    task_id: str
    status: str


@dataclass(frozen=True)
class HourlyDelta:
    transitions: tuple[tuple[datetime, str], ...]
    new_artifacts: int

    @property
    def unchanged(self) -> bool:
        return not self.transitions and self.new_artifacts == 0


@dataclass(frozen=True)
class CardDetails:
    artifact: Artifact | None
    executor: Executor | None
    next_action: str
    dependencies: tuple[DependencyState, ...]
    delta: HourlyDelta


def safe_text(value: str | None, limit: int = TEXT_LIMIT) -> str:
    """Redacted and truncated; HTML escaping stays with the renderer."""
    text = redact_view((value or "").strip())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def ref_links(refs: Any) -> tuple[RefLink, ...]:
    out: list[RefLink] = []
    for item in refs if isinstance(refs, list) else []:
        if not isinstance(item, Mapping) or not item.get("kind") or not item.get("ref"):
            continue
        kind, ref = str(item["kind"]), redact_view(str(item["ref"]))
        url = ref if kind == "url" and ref.lower().startswith(("http://", "https://")) else None
        out.append(RefLink(kind, safe_text(ref, 200), url))
        if len(out) == REF_LIMIT:
            break
    return tuple(out)


def artifact_of(event: Any, row: Any) -> Artifact | None:
    """Latest result event; its own refs when it carries them, else the task refs."""
    if event is None:
        return None
    own = (event.data or {}).get("refs")
    refs = ref_links(own if own else getattr(row, "refs", None))
    return Artifact(event.at, event.kind, safe_text(event.text), refs)


def executor_of(row: Any, now: datetime) -> Executor | None:
    if row.lease_holder and row.lease_until and row.lease_until > now:
        return Executor(row.lease_holder, EXECUTOR_LABELS["lease"])
    if getattr(row, "responsible", None):
        return Executor(row.responsible, EXECUTOR_LABELS["responsible"])
    if getattr(row, "owner", None):
        return Executor(row.owner, EXECUTOR_LABELS["owner"])
    return None


def dependencies_of(row: Any, infos: Mapping[tuple[str, str], StatusInfo]
                    ) -> tuple[DependencyState, ...]:
    out = []
    for dep in getattr(row, "depends_on", None) or []:
        info = infos.get((row.room, str(dep)))
        out.append(DependencyState(str(dep), info.status if info else "не найдена"))
    return tuple(out)


def delta_of(transitions: list[Any], result_events: list[Any], now: datetime) -> HourlyDelta:
    start = now - timedelta(hours=1)
    moves = sorted((e.at, TRANSITION_LABELS.get(e.kind, e.kind)) for e in transitions
                   if start <= e.at <= now and e.kind in TRANSITION_KINDS)
    return HourlyDelta(tuple(moves), sum(start <= e.at <= now for e in result_events))


def build_details(row: Any, last: Any, now: datetime,
                  infos: Mapping[tuple[str, str], StatusInfo],
                  transitions: list[Any], result_events: list[Any]) -> CardDetails:
    return CardDetails(artifact_of(last, row), executor_of(row, now),
                       safe_text(getattr(row, "next_action", None)), dependencies_of(row, infos),
                       delta_of(transitions, result_events, now))
