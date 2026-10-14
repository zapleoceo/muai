"""Read-side scope filter for the Office report; never touches Room data."""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

from dashboard.office_card_details import HIDDEN_DEPENDENCY
from dashboard.office_status import StatusInfo

log = logging.getLogger(__name__)
DEFAULT_EXCLUDE_PREFIXES = ("SIN-", "STEP-")
DEFAULT_EXCLUDE_PROJECTS = ("Sintegrum", "ИИ-звонилка", "SIN", "STEP")
DEFAULT_ACTIVE_ONLY_PROJECTS = ("Orchestra", "Lamas", "SinHRM")
DEFAULT_ACTIVE_WINDOW_HOURS = 72
OUR_AGENTS = ("claude", "codex", "dot")
_AGENT_RE = re.compile(r"(?<!\w)(?:" + "|".join(OUR_AGENTS) + r")(?!\w)", re.IGNORECASE)


@dataclass(frozen=True)
class ScopeConfig:
    exclude_prefixes: tuple[str, ...] = DEFAULT_EXCLUDE_PREFIXES
    exclude_projects: frozenset[str] = frozenset(p.casefold() for p in DEFAULT_EXCLUDE_PROJECTS)
    active_only_projects: frozenset[str] = frozenset(
        p.casefold() for p in DEFAULT_ACTIVE_ONLY_PROJECTS)
    active_window: timedelta = timedelta(hours=DEFAULT_ACTIVE_WINDOW_HOURS)
    active_only_names: tuple[str, ...] = DEFAULT_ACTIVE_ONLY_PROJECTS


@dataclass(frozen=True)
class ScopeSplit:
    visible: list[Any]
    hidden_excluded: int
    hidden_inactive: int
    hidden_keys: frozenset[tuple[str, str]] = frozenset()


def _csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def config_from_env(environ: Mapping[str, str]) -> ScopeConfig:
    hours = environ.get("OFFICE_ACTIVE_WINDOW_HOURS")
    window = None
    if hours not in (None, ""):
        try:
            window = timedelta(hours=float(hours))
        except ValueError:
            log.warning("OFFICE_ACTIVE_WINDOW_HOURS=%r invalid, using %s",
                        hours, DEFAULT_ACTIVE_WINDOW_HOURS)
    return ScopeConfig(
        exclude_prefixes=tuple(p.upper() for p in _csv(
            environ.get("OFFICE_EXCLUDE_PREFIXES"), DEFAULT_EXCLUDE_PREFIXES)),
        exclude_projects=frozenset(p.casefold() for p in _csv(
            environ.get("OFFICE_EXCLUDE_PROJECTS"), DEFAULT_EXCLUDE_PROJECTS)),
        active_only_projects=frozenset(p.casefold() for p in _csv(
            environ.get("OFFICE_ACTIVE_ONLY_PROJECTS"), DEFAULT_ACTIVE_ONLY_PROJECTS)),
        active_only_names=_csv(
            environ.get("OFFICE_ACTIVE_ONLY_PROJECTS"), DEFAULT_ACTIVE_ONLY_PROJECTS),
        active_window=window or timedelta(hours=DEFAULT_ACTIVE_WINDOW_HOURS))


@cache
def env_config() -> ScopeConfig:
    return config_from_env(os.environ)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _project_key(project: str | None) -> str:
    return (project or "").strip().casefold()


def is_excluded(row: Any, cfg: ScopeConfig) -> bool:
    task_id = (row.task_id or "").upper()
    if any(task_id.startswith(p) for p in cfg.exclude_prefixes):
        return True
    return _project_key(row.project) in cfg.exclude_projects


PAUSED_STATUSES = frozenset({"blocked", "waiting", "paused"})


def is_parked(row: Any, now: datetime, control: str | None) -> bool:
    """Waiting, blocked or paused: not in-progress work, whatever the lease says."""
    until = getattr(row, "waiting_until", None)
    return bool(row.status in PAUSED_STATUSES or control in PAUSED_STATUSES
                or getattr(row, "waiting_reason", None)
                or (until is not None and _aware(until) > _aware(now)))


def is_our_current_work(row: Any, now: datetime, cfg: ScopeConfig,
                        control: str | None = None) -> bool:
    if is_parked(row, now, control):
        return False
    if row.lease_until is not None and _aware(row.lease_until) > _aware(now):
        return bool(_AGENT_RE.search(row.lease_holder or ""))
    if row.lease_holder and not _AGENT_RE.search(row.lease_holder):
        return False
    progress = row.last_progress_at
    if progress is None or _aware(now) - _aware(progress) > cfg.active_window:
        return False
    return bool(_AGENT_RE.search(f"{row.lease_holder or ''} {row.responsible or ''}"))


def apply_scope(rows: list[Any], now: datetime, cfg: ScopeConfig | None = None,
                controls: Mapping[tuple[str, str], str] | None = None) -> ScopeSplit:
    cfg = cfg or env_config()
    visible: list[Any] = []
    excluded = inactive = 0
    hidden_keys: set[tuple[str, str]] = set()
    for row in rows:
        open_row = row.status not in ("done", "cancelled")
        if is_excluded(row, cfg):
            excluded += open_row
            hidden_keys.add((row.room, row.task_id))
        elif (_project_key(row.project) in cfg.active_only_projects
              and not is_our_current_work(row, now, cfg, (controls or {}).get((row.room, row.task_id)))):
            inactive += open_row
            hidden_keys.add((row.room, row.task_id))
        else:
            visible.append(row)
    return ScopeSplit(visible, excluded, inactive, frozenset(hidden_keys))


def _runtime_excluded(entry: Any, cfg: ScopeConfig) -> bool:
    ids = ((entry.source_id or "").upper(), (entry.thread_id or "").upper())
    return (_project_key(entry.project) in cfg.exclude_projects
            or any(i.startswith(p) for i in ids for p in cfg.exclude_prefixes))


def scope_runtime(snapshot: Any, cfg: ScopeConfig | None = None) -> tuple[Any, int]:
    """Runtime streams are only subject to the project exclusion, not the active-only rule."""
    if snapshot is None:
        return None, 0
    cfg = cfg or env_config()
    kept = tuple(e for e in snapshot.entries if not _runtime_excluded(e, cfg))
    hidden = len(snapshot.entries) - len(kept)
    return (replace(snapshot, entries=kept) if hidden else snapshot), hidden


def mask_hidden_deps(row: Any, info: StatusInfo, hidden: frozenset[tuple[str, str]]) -> StatusInfo:
    """Rebuild the "зависит от" part from depends_on so a hidden id never reaches the page."""
    prefix = "зависит от: "
    if not info.dependency or not any((row.room, str(d)) in hidden for d in row.depends_on or []):
        return info
    names = ", ".join(HIDDEN_DEPENDENCY if (row.room, str(d)) in hidden else str(d)
                      for d in row.depends_on)
    parts = [prefix + names if p.startswith(prefix) else p for p in info.dependency.split("; ")]
    return replace(info, dependency="; ".join(parts))
