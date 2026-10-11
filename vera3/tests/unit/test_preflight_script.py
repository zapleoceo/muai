"""scripts/preflight.sh: режимы гейт / partial и коды возврата.

Скрипт гоняется в одноразовом git-репозитории с локальным «origin»; python
подменён заглушкой, поэтому проверяется логика скрипта, а не сам pytest/ruff.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="нужен bash")

STUB_PY = """#!/usr/bin/env bash
# Заглушка python: STUB_MISSING — список через запятую: pytest_cov, diff_cover.
case "$1" in
  -c) [[ ",${STUB_MISSING:-}," == *,pytest_cov,* ]] && exit 1; exit 0 ;;
  -m)
    case "$2" in
      diff_cover.diff_cover_tool) [[ ",${STUB_MISSING:-}," == *,diff_cover,* ]] && exit 1; exit 0 ;;
      pytest) for a in "$@"; do [[ "$a" == --cov-report=xml:* ]] && : > coverage.xml; done; exit 0 ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "master")
    dest = work / "vera3" / "scripts"
    dest.mkdir(parents=True)
    for name in ["preflight.sh", *[p.name for p in SCRIPTS.glob("ci_gate_*.sh")]]:
        shutil.copy(SCRIPTS / name, dest / name)
    (work / "vera3" / "docs").mkdir()
    (work / "vera3" / "docs" / "a.md").write_text("a\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "push", "-q", "origin", "master")
    _git(work, "checkout", "-q", "-b", "feat")
    (work / "vera3" / "docs" / "b.md").write_text("b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "change")
    return work


@pytest.fixture
def stub_py(tmp_path: Path) -> Path:
    p = tmp_path / "stubpy"
    p.write_text(STUB_PY, encoding="utf-8", newline="\n")
    p.chmod(0o755)
    return p


def _run(repo: Path, stub: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    full = {**os.environ, "PREFLIGHT_PY": stub.as_posix(), **env}
    for k in ("PREFLIGHT_PG_URL", "STUB_MISSING"):
        if k not in env:
            full.pop(k, None)
    return subprocess.run(
        [BASH, "vera3/scripts/preflight.sh", *args],
        cwd=repo, env=full, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


PG = {"PREFLIGHT_PG_URL": "postgresql+asyncpg://u:p@localhost/db"}


def test_full_set_passing_gives_pass(repo: Path, stub_py: Path) -> None:
    r = _run(repo, stub_py, **PG)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESULT: PASS" in r.stdout


def test_fetch_failure_is_blocker(repo: Path, stub_py: Path) -> None:
    _git(repo, "remote", "set-url", "origin", str(repo / "no-such-remote"))
    r = _run(repo, stub_py, **PG)
    assert r.returncode != 0
    assert "stale base: fetch failed" in r.stdout
    assert "RESULT: PASS" not in r.stdout


def test_missing_pg_url_is_incomplete(repo: Path, stub_py: Path) -> None:
    r = _run(repo, stub_py)
    assert r.returncode == 3, r.stdout
    assert "RESULT: INCOMPLETE" in r.stdout
    assert "PREFLIGHT_PG_URL" in r.stdout


@pytest.mark.parametrize("tool", ["pytest_cov", "diff_cover"])
def test_missing_dependency_is_incomplete(repo: Path, stub_py: Path, tool: str) -> None:
    r = _run(repo, stub_py, STUB_MISSING=tool, **PG)
    assert r.returncode == 3, r.stdout
    assert "RESULT: INCOMPLETE" in r.stdout
    assert "RESULT: PASS" not in r.stdout


def test_partial_never_prints_pass(repo: Path, stub_py: Path) -> None:
    r = _run(repo, stub_py, "--partial", **PG)
    assert r.returncode == 0, r.stdout
    assert "RESULT: PARTIAL" in r.stdout
    assert "non-gating" in r.stdout
    assert "RESULT: PASS" not in r.stdout


def test_partial_without_pg_states_ci_authoritative(repo: Path, stub_py: Path) -> None:
    r = _run(repo, stub_py, "--partial")
    assert r.returncode == 0, r.stdout
    assert "PG integration: not run — CI authoritative" in r.stdout
    assert "RESULT: PASS" not in r.stdout


def test_fast_implies_partial(repo: Path, stub_py: Path) -> None:
    r = _run(repo, stub_py, "--fast")
    assert "RESULT: PARTIAL" in r.stdout
    assert "RESULT: PASS" not in r.stdout
