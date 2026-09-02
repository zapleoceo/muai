"""Монитор не должен слать пару сообщений на каждый скачок метрики.

02.09.2026 память на хосте прыгала 31% ↔ 95% каждые 10-20 минут (локальная
vision-модель брокера грузит ~5 ГБ, ловит OOM, грузится снова). На каждый
скачок уходила пара «⚠️ RAM 93%» → «✅ RAM back to 31%»: четырнадцать
сообщений за пять часов при ОДНОЙ непрерывной аварии.

Дыра была в `recover()`: он срабатывал по первой же удачной выборке и стирал
state-файл, который заодно служит отметкой throttle, — то есть каждый цикл
alert→recover→alert начинался с чистого листа. Правка 06.08.2026 закрыла
только сторону алерта.

Скрипт гоняется по-настоящему, с подставными curl/logger: логика живёт в bash.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

MONITOR = Path(__file__).resolve().parents[2] / "scripts" / "vera3-monitor.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


def _run(tmp_path: Path, steps: list[str]) -> list[str]:
    """Прогнать последовательность проверок; вернуть ушедшие в TG сообщения."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    sent = tmp_path / "sent.txt"
    for name, body in (
        ("curl", f'echo "$@" >> "{sent.as_posix()}"\nexit 0\n'),
        ("logger", "exit 0\n"),
    ):
        path = bindir / name
        path.write_bytes(f"#!/bin/bash\n{body}".encode())
        path.chmod(0o755)

    env_file = tmp_path / "env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=t\nOWNER_TELEGRAM_ID=1\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir.as_posix()}{os.pathsep}{env['PATH']}"
    env["ENV_FILE"] = str(env_file)
    env["STATE_DIR"] = str(tmp_path / "state")
    subprocess.run(["bash", str(MONITOR), "--selftest-notify", *steps],
                   capture_output=True, text=True, env=env, timeout=60, check=True)
    if not sent.exists():
        return []
    out = []
    for line in sent.read_text(encoding="utf-8").splitlines():
        if "monitor" in line:
            out.append("alert")
        elif "recovered" in line:
            out.append("recover")
    return out


def test_single_blink_says_nothing(tmp_path):
    """Одна упавшая проверка — ещё не авария (monitor_fail_streak=2)."""
    assert _run(tmp_path, ["fail", "ok"]) == []


def test_sustained_failure_alerts_once(tmp_path):
    assert _run(tmp_path, ["fail", "fail", "fail", "fail"]) == ["alert"]


def test_recovery_needs_a_streak_not_one_good_sample(tmp_path):
    """Ровно баг 02.09: авария мигает, и на каждый проблеск уходит «✅»."""
    steps = ["fail", "fail"] + ["ok", "fail", "fail"] * 3
    assert _run(tmp_path, steps) == ["alert"]


def test_recovery_is_announced_after_enough_calm(tmp_path):
    assert _run(tmp_path, ["fail", "fail", "ok", "ok", "ok"]) == ["alert", "recover"]


def test_alert_can_fire_again_after_a_real_recovery(tmp_path):
    """Гашение не должно запирать монитор: новая авария обязана прозвучать."""
    steps = ["fail", "fail", "ok", "ok", "ok", "fail", "fail"]
    assert _run(tmp_path, steps) == ["alert", "recover", "alert"]
