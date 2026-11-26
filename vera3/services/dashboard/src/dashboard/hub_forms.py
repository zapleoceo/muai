"""Разбор форм админки хаба в объекты общего слоя (без записи в БД)."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from vera_shared.hub.names import HubNameError
from vera_shared.hub.spec import UpstreamSpec


def _text(form: Mapping[str, Any], key: str, limit: int = 2000) -> str:
    return str(form.get(key) or "").strip()[:limit]


def _int(form: Mapping[str, Any], key: str, default: int) -> int:
    raw = _text(form, key, 8)
    try:
        return int(raw) if raw else default
    except ValueError as exc:
        raise HubNameError(f"{key} must be a number") from exc


def form_id(form: Mapping[str, Any], key: str = "id") -> int | None:
    raw = _text(form, key, 20)
    return int(raw) if raw.isdigit() else None


def upstream_from_form(form: Mapping[str, Any]) -> UpstreamSpec:
    command = [line.strip() for line in _text(form, "command").splitlines() if line.strip()]
    return UpstreamSpec(
        project=_text(form, "project", 16), slug=_text(form, "slug", 16).lower(),
        title=_text(form, "title", 128), transport=_text(form, "transport", 8) or "http",
        url=_text(form, "url"), command=command,
        auth_kind=_text(form, "auth_kind", 8) or "none", auth_header=_text(form, "auth_header", 64),
        enabled=form.get("enabled") == "on", hint=_text(form, "hint"),
        timeout_s=_int(form, "timeout_s", 30), max_concurrency=_int(form, "max_concurrency", 4),
    ).validate()


def secret_from_form(form: Mapping[str, Any]) -> tuple[str | None, bool]:
    """(новый секрет или None — оставить прежний, стереть ли секрет)."""
    secret = str(form.get("secret") or "").strip()
    return (secret or None), form.get("clear_secret") == "on"


def grants_from_form(values: list[str]) -> list[str]:
    return [v for v in (s.strip() for s in values) if v]
