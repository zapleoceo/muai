"""mcp при деплое дожидается текущих запросов, а не обрывает их SIGKILL'ом."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
MAIN = (ROOT / "services" / "mcp" / "src" / "vera_mcp" / "__main__.py").read_text(
    encoding="utf-8")


def _mcp_block() -> str:
    m = re.search(r"^  mcp:\n(.*?)(?=^  \S|\Z)", COMPOSE, re.S | re.M)
    assert m, "сервис mcp не найден в compose"
    return m.group(1)


def test_grace_period_exceeds_uvicorn_drain() -> None:
    drain = int(re.search(r"^GRACEFUL_SHUTDOWN_S = (\d+)", MAIN, re.M).group(1))
    grace = int(re.search(r"stop_grace_period:\s*(\d+)s", _mcp_block()).group(1))
    assert grace >= drain + 5


def test_uvicorn_gets_graceful_timeout() -> None:
    assert "timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S" in MAIN


def test_mcp_stops_with_sigterm() -> None:
    assert "stop_signal: SIGTERM" in _mcp_block()
