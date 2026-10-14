"""Read-side scope filter for the Office report; never touches Room data."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

DEFAULT_EXCLUDE_PREFIXES = ("SIN-", "STEP-")
DEFAULT_EXCLUDE_PROJECTS = ("Sintegrum", "ИИ-звонилка")
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


@dataclass(frozen=True)
class ScopeSplit:
    visible: list[Any]
    hidden_excluded: int
    hidden_inactive: int


def _csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def config_from_env(environ: Mapping[str, str]) -> ScopeConfig:
    hours = environ.get("OFFICE_ACTIVE_WINDOW_HOURS")
    try:
        window = timedelta(hours=float(hours)) if hours not in (None, "") else None
    except ValueError:
        window = None
    return ScopeConfig(
        exclude_prefixes=tuple(p.upper() for p in _csv(
            environ.get("OFFICE_EXCLUDE_PREFIXES"), DEFAULT_EXCLUDE_PREFIXES)),
        exclude_projects=frozenset(p.casefold() for p in _csv(
            environ.get("OFFICE_EXCLUDE_PROJECTS"), DEFAULT_EXCLUDE_PROJECTS)),
        active_only_projects=frozenset(p.casefold() for p in _csv(
            environ.get("OFFICE_ACTIVE_ONLY_PROJECTS"), DEFAULT_ACTIVE_ONLY_PROJECTS)),
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


def is_our_current_work(row: Any, now: datetime, cfg: ScopeConfig) -> bool:
    if row.lease_until is not None and _aware(row.lease_until) > _aware(now):
        return True
    progress = row.last_progress_at
    if progress is None or _aware(now) - _aware(progress) > cfg.active_window:
        return False
    return bool(_AGENT_RE.search(f"{row.lease_holder or ''} {row.responsible or ''}"))


def apply_scope(rows: list[Any], now: datetime, cfg: ScopeConfig | None = None) -> ScopeSplit:
    cfg = cfg or env_config()
    visible: list[Any] = []
    excluded = inactive = 0
    for row in rows:
        open_row = row.status not in ("done", "cancelled")
        if is_excluded(row, cfg):
            excluded += open_row
        elif (_project_key(row.project) in cfg.active_only_projects
              and not is_our_current_work(row, now, cfg)):
            inactive += open_row
        else:
            visible.append(row)
    return ScopeSplit(visible, excluded, inactive)


def scope_runtime(snapshot: Any, cfg: ScopeConfig | None = None) -> tuple[Any, int]:
    """Runtime streams are only subject to the project exclusion, not the active-only rule."""
    if snapshot is None:
        return None, 0
    cfg = cfg or env_config()
    kept = tuple(e for e in snapshot.entries if _project_key(e.project) not in cfg.exclude_projects)
    hidden = len(snapshot.entries) - len(kept)
    return (replace(snapshot, entries=kept) if hidden else snapshot), hidden
