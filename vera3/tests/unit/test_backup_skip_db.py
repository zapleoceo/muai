"""vera-backup.sh: дамп БД Vera перенесён в NAS-поток, секреты и остальные проекты — как раньше."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "vera-backup.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="нужен bash и GNU coreutils (Linux)",
)

CONTAINERS = ("aibroker-postgres", "stepan2-postgres", "vera3-postgres")
ENV_FILES = ("aibroker/.env", "stepan2/infra/.env", "vera3/infra/.env")


def _setup(tmp: Path) -> tuple[Path, Path, Path]:
    dest, www, bindir = tmp / "dest", tmp / "www", tmp / "bin"
    bindir.mkdir()
    for rel in ENV_FILES:
        f = www / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("TOKEN_SECRET=x\n", encoding="utf-8")
    docker = bindir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = ps ]; then printf "%s\n" '
        + " ".join(CONTAINERS)
        + "; exit 0; fi\n"
        'echo "fake-pg_dump $*"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return dest, www, bindir


def _run(tmp: Path, **extra: str) -> tuple[Path, str]:
    dest, www, bindir = _setup(tmp)
    env = dict(
        os.environ,
        DEST=str(dest),
        WWW_ROOT=str(www),
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        BACKUP_GROUP="root",
        FULL_DOW=str(time.localtime().tm_wday + 1),  # weekly сегодня
    )
    env.pop("SKIP_DB_PROJECTS", None)
    env.update(extra)
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
        check=True,
    )
    return dest, r.stdout


def _names(d: Path) -> set[str]:
    return {p.name for p in d.iterdir()} if d.is_dir() else set()


def _today(dest: Path, project: str) -> Path:
    (day,) = (dest / project / "daily").iterdir()
    return day


def test_default_skips_vera_db_keeps_secrets(tmp_path: Path) -> None:
    dest, out = _run(tmp_path)
    assert _names(_today(dest, "vera")) == {"secrets.tar.gz", "SHA256SUMS"}
    assert not (dest / "vera" / "weekly").exists()
    assert "db dump of vera moved to NAS stream (vera-stream), skipped" in out


def test_other_projects_unchanged(tmp_path: Path) -> None:
    dest, _ = _run(tmp_path)
    assert _names(_today(dest, "aibroker")) == {
        "aibroker.dump",
        "secrets.tar.gz",
        "SHA256SUMS",
    }
    assert _names(_today(dest, "stepan2")) == {
        "stepan2.dump",
        "secrets.tar.gz",
        "SHA256SUMS",
    }


def test_empty_skip_list_restores_vera_dump_and_weekly(tmp_path: Path) -> None:
    dest, out = _run(tmp_path, SKIP_DB_PROJECTS="")
    assert _names(_today(dest, "vera")) == {"vera.dump", "secrets.tar.gz", "SHA256SUMS"}
    (week,) = (dest / "vera" / "weekly").iterdir()
    assert _names(week) == {"vera.dump", "SHA256SUMS"}
    assert "skipped" not in out
