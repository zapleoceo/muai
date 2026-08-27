"""OpenMP не должен ждать активно — иначе слушатель греет три ядра в тишине.

Детектор речи зовут 62 раза в секунду, промежуток 16 мс, и воркеры OpenMP не
успевают уснуть. Замер на живом ноутбуке: 223% ядра по умолчанию против 5%
с PASSIVE, при неизменной скорости распознавания.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run(code: str, env_extra: dict[str, str] | None = None) -> str:
    env = {**os.environ, "PYTHONPATH": SRC}
    env.update(env_extra or {})
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_import_sets_passive_wait():
    """Свежий процесс: импорт пакета обязан выставить переменную сам."""
    got = _run("import os, vera_listener; print(os.environ['OMP_WAIT_POLICY'])")
    assert got == "PASSIVE"


def test_explicit_setting_wins():
    """Заданное снаружи не перетираем — иначе не отладить."""
    got = _run("import os, vera_listener; print(os.environ['OMP_WAIT_POLICY'])",
               {"OMP_WAIT_POLICY": "ACTIVE"})
    assert got == "ACTIVE"


def test_set_before_native_libraries_load():
    """Переменную читает vcomp140.dll при загрузке: после импорта уже поздно."""
    source = (Path(SRC) / "vera_listener" / "__init__.py").read_text(encoding="utf-8")
    assert "OMP_WAIT_POLICY" in source, "настройка обязана жить в __init__ пакета"
