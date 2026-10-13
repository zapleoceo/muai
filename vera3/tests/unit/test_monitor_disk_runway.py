"""Прогноз «дней до 92% диска» в vera3-monitor.sh (--project-days)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

MONITOR = Path(__file__).resolve().parents[2] / "scripts" / "vera3-monitor.sh"
GB = 1048576  # КБ в ГБ

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


def _days(tmp_path: Path, hist: str, now: int, used_gb: float, total_gb: float) -> int:
    f = tmp_path / "hist"
    f.write_text(hist, encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "logger").write_bytes(b"#!/bin/bash\nexit 0\n")
    (bindir / "logger").chmod(0o755)
    env_file = tmp_path / "env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=t\nOWNER_TELEGRAM_ID=1\n", encoding="utf-8")
    env = dict(os.environ, ENV_FILE=str(env_file), STATE_DIR=str(tmp_path / "state"))
    env["PATH"] = f"{bindir.as_posix()}{os.pathsep}{env['PATH']}"
    r = subprocess.run(
        ["bash", str(MONITOR), "--project-days", str(f), str(now),
         str(int(used_gb * GB)), str(int(total_gb * GB))],
        capture_output=True, text=True, timeout=30, check=True, env=env)
    return int(r.stdout.strip())


def test_default_rate_without_history(tmp_path: Path) -> None:
    # 72 ГБ, занято 57: до 92% = 9.24 ГБ, при 0.45 ГБ/сутки = 20 суток
    assert _days(tmp_path, "", 1_000_000, 57, 72) == 20


def test_measured_rate_from_history(tmp_path: Path) -> None:
    now = 10 * 86400
    hist = f"{now - 2 * 86400} {int(55 * GB)}\n"  # +1 ГБ/сутки
    assert _days(tmp_path, hist, now, 57, 72) == 9


def test_short_history_falls_back_to_default(tmp_path: Path) -> None:
    now = 10 * 86400
    hist = f"{now - 3600} {int(50 * GB)}\n"
    assert _days(tmp_path, hist, now, 57, 72) == 20


def test_over_limit_is_zero(tmp_path: Path) -> None:
    assert _days(tmp_path, "", 1_000_000, 70, 72) == 0
