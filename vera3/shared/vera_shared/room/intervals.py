"""Фактические интервалы выполнения задачи из журнала событий (для диаграммы Ганта).

Берутся только метки событий, `lease_until` и `now`; прошлое не достраивается.
Дорожка (lane) — пара агент+сессия. Событие без агента (`lease_expired`) закрывает все
открытые дорожки. `claimed` и `handoff_accept` закрывают чужие открытые дорожки.
Состояние меняют: claimed/resumed/unblocked/handoff_accept/ack_answer -> work;
paused, review, blocked/question, waiting; progress/heartbeat лишь открывают work, если
дорожка пуста или срок waiting истёк, и не выводят из паузы, проверки и блока. released/done/lease_expired закрывают.
Конец открытого отрезка: аренда жива (`lease_until > now`) — `now`, иначе `lease_until`,
если он позже начала, иначе последнее событие (нулевой отрезок отбрасывается). `waiting`
режется по `until_seconds` из `data`; провал между этим сроком и следующим событием той же
дорожки — `unknown` (единственное место, где он рисуется).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

WORK, PAUSED, REVIEW, BLOCKED, WAITING, UNKNOWN = (
    "work", "paused", "review", "blocked", "waiting", "unknown")
SEGMENT_KINDS = (WORK, PAUSED, REVIEW, BLOCKED, WAITING, UNKNOWN)

_TO_WORK = {"claimed", "resumed", "unblocked", "handoff_accept", "ack_answer"}
_OPEN_WORK = {"progress", "heartbeat"}
_STATE = {"paused": PAUSED, "review": REVIEW, "blocked": BLOCKED, "question": BLOCKED,
          "waiting": WAITING}
_CLOSE = {"released", "done", "lease_expired"}
_EXCLUSIVE = {"claimed", "handoff_accept"}

_Lane = tuple[str, str]


@dataclass(frozen=True)
class Segment:
    agent: str
    session: str
    kind: str
    start: datetime
    end: datetime


@dataclass
class _Open:
    kind: str
    start: datetime
    cap: datetime | None = None


def _cap(e: Any) -> datetime | None:
    secs = (getattr(e, "data", None) or {}).get("until_seconds")
    if isinstance(secs, int | float) and not isinstance(secs, bool) and secs > 0:
        return e.at + timedelta(seconds=secs)
    return None


class _Builder:
    def __init__(self) -> None:
        self.out: list[Segment] = []
        self.open: dict[_Lane, _Open] = {}
        self.gap_from: dict[_Lane, datetime] = {}

    def add(self, lane: _Lane, kind: str, start: datetime, end: datetime) -> None:
        if end > start:
            self.out.append(Segment(lane[0], lane[1], kind, start, end))

    def close(self, lane: _Lane, at: datetime, *, keep_gap: bool = False) -> None:
        cur = self.open.pop(lane, None)
        if cur is None:
            return
        end = min(at, cur.cap) if cur.cap else at
        self.add(lane, cur.kind, cur.start, end)
        if keep_gap and cur.cap and cur.cap < at:
            self.gap_from[lane] = cur.cap

    def start(self, lane: _Lane, kind: str, e: Any) -> None:
        gap = self.gap_from.pop(lane, None)
        if gap is not None:
            self.add(lane, UNKNOWN, gap, e.at)
        self.open[lane] = _Open(kind, e.at, _cap(e) if kind == WAITING else None)


def _apply(b: _Builder, e: Any) -> None:
    if e.kind in _CLOSE:
        for lane in ([(e.agent, e.session or "")] if e.agent else list(b.open)):
            b.close(lane, e.at)
        return
    if not e.agent:
        return
    lane = (e.agent, e.session or "")
    if e.kind in _EXCLUSIVE:
        for other in [x for x in b.open if x != lane]:
            b.close(other, e.at)
    cur = b.open.get(lane)
    target = WORK if e.kind in _TO_WORK else _STATE.get(e.kind)
    if target is None and e.kind in _OPEN_WORK and (
            cur is None or (cur.kind == WAITING and cur.cap and e.at >= cur.cap)):
        target = WORK
    if target is None or (cur and cur.kind == target and target != WAITING):
        return
    if cur:
        b.close(lane, e.at, keep_gap=True)
    b.start(lane, target, e)


def build_segments(events: Iterable[Any], *, now: datetime,
                   lease_until: datetime | None = None) -> list[Segment]:
    """События упорядочены по id; datetime — наивный UTC. Нет событий — нет отрезков."""
    b = _Builder()
    last: datetime | None = None
    for e in events:
        _apply(b, e)
        last = e.at
    live = lease_until is not None and lease_until > now
    for lane, cur in list(b.open.items()):
        if live:
            end = now
        elif lease_until is not None and lease_until > cur.start:
            end = lease_until
        else:
            end = last or cur.start
        b.close(lane, end)
    return sorted(b.out, key=lambda s: (s.start, s.agent, s.session))
