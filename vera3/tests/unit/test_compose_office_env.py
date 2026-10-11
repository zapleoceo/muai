"""mcp в docker-compose получает office-переменные; пустой токен-реестр сохраняет поведение (401)."""
from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "infra" / "docker-compose.yml"


def test_mcp_environment_has_office_vars() -> None:
    env = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["mcp"]["environment"]
    assert env["OFFICE_SERVICE_TOKENS"] == "${OFFICE_SERVICE_TOKENS:-}"
    assert env["OFFICE_ROOM"] == "${OFFICE_ROOM:-main}"
    assert env["OFFICE_AUTO_PICKUP"] == "${OFFICE_AUTO_PICKUP:-0}"
