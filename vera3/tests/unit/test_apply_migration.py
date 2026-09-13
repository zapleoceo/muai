"""apply_migration.sh не записывает в учёт миграцию, оставившую INVALID-индекс.

CREATE INDEX CONCURRENTLY, прерванный снаружи, оставляет невалидный индекс, а
повторный накат с IF NOT EXISTS его молча пропускает. Без проверки такая
миграция попадала бы в schema_migrations как успешная (ревью 13.09.2026).
Скрипт гоняется по-настоящему, docker подменён.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "apply_migration.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")


def _run(tmp_path: Path, *, invalid: str) -> tuple[int, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "sql.log"
    stub = f"""#!/bin/bash
q="${{@: -1}}"
echo "$q" >> "{log.as_posix()}"
case "$q" in
  *to_regclass*) echo t ;;
  *"FROM schema_migrations WHERE version"*) echo f ;;
  *indisvalid*) printf '%s' "{invalid}" ;;
  *) cat >/dev/null 2>&1 || true ;;
esac
exit 0
"""
    (bindir / "docker").write_bytes(stub.encode())
    (bindir / "docker").chmod(0o755)
    mig = tmp_path / "029_usage_log_created_at_index.sql"
    mig.write_text("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_usage_created_at ON usage_log (created_at);\n",
                   encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bindir.as_posix()}{os.pathsep}{env['PATH']}"
    done = subprocess.run(["bash", str(SCRIPT), str(mig)], capture_output=True,
                          text=True, env=env, timeout=60)
    recorded = log.exists() and "INSERT INTO schema_migrations" in log.read_text(encoding="utf-8")
    return done.returncode, "recorded" if recorded else "not-recorded"


def test_invalid_index_blocks_recording(tmp_path):
    code, state = _run(tmp_path, invalid="ix_usage_created_at")
    assert code == 1
    assert state == "not-recorded"


def test_clean_run_is_recorded(tmp_path):
    code, state = _run(tmp_path, invalid="")
    assert code == 0
    assert state == "recorded"
