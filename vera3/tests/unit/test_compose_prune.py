"""Уборка диска: висячие образы раз в час, кэш сборки раз в сутки, никогда -a."""
from __future__ import annotations

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "infra" / "docker-compose.yml"


def _prune_command() -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    block = text.split("\n  prune:\n", 1)[1].split("\nvolumes:", 1)[0]
    return block.split("entrypoint:", 1)[1].split("mem_limit", 1)[0]


def test_image_prune_hourly_without_all_flag() -> None:
    cmd = _prune_command()
    assert "sleep 3600" in cmd
    assert "docker image prune -f" in cmd
    assert not re.search(r"prune\s+(-\w*a|--all)", cmd)
    assert "system prune" not in cmd


def test_builder_prune_daily_with_age_filter() -> None:
    cmd = _prune_command()
    assert "n % 24" in cmd
    assert "docker builder prune -f --filter until=72h" in cmd
