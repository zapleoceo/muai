"""Prune удаляет дамп только при явном подтверждении NAS, не по atime."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "vera-backup-prune.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="нужен bash и GNU stat (Linux)",
)

OLD = time.time() - 3 * 3600


def _make(root: Path, name: str = "vera.dump", *, listed: bool = True,
          atime_read: bool = True, settled: bool = True) -> Path:
    d = root / "vera" / "daily" / "2026-10-13"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    data = b"payload-" + name.encode()
    f.write_bytes(data)
    if listed:
        sha = hashlib.sha256(data).hexdigest()
        with (d / "SHA256SUMS").open("a", encoding="utf-8") as s:
            s.write(f"{sha}  ./{name}\n")
    mtime = OLD if settled else time.time()
    atime = mtime + 600 if atime_read else mtime
    os.utime(f, (atime, mtime))
    return f


def _confirm(f: Path, sha: str | None = None) -> None:
    sha = sha if sha is not None else hashlib.sha256(f.read_bytes()).hexdigest()
    Path(str(f) + ".nas-confirmed").write_text(sha + "\n", encoding="utf-8")


def _run(root: Path) -> str:
    env = dict(os.environ, ROOT=str(root), SETTLE_MIN="30")
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, encoding="utf-8",
                       env=env, timeout=60, check=True)
    return r.stdout


def test_read_but_unconfirmed_is_kept(tmp_path: Path) -> None:
    f = _make(tmp_path)
    out = _run(tmp_path)
    assert f.exists()
    assert "no_confirm=1" in out


def test_aborted_partial_transfer_is_kept(tmp_path: Path) -> None:
    f = _make(tmp_path)
    os.utime(f, (OLD + 70, OLD))  # прочитано 70 секунд, подтверждения нет
    _run(tmp_path)
    assert f.exists()


def test_wrong_sha_confirmation_is_kept(tmp_path: Path) -> None:
    f = _make(tmp_path)
    _confirm(f, "0" * 64)
    out = _run(tmp_path)
    assert f.exists()
    assert "mismatch=1" in out


def test_empty_confirmation_is_kept(tmp_path: Path) -> None:
    f = _make(tmp_path)
    _confirm(f, "")
    assert "empty_confirm=1" in _run(tmp_path)
    assert f.exists()


def test_correct_confirmation_settled_is_deleted(tmp_path: Path) -> None:
    f = _make(tmp_path)
    _confirm(f)
    sums = f.parent / "SHA256SUMS"
    conf = Path(str(f) + ".nas-confirmed")
    _run(tmp_path)
    assert not f.exists()
    assert sums.exists()
    assert conf.exists()


def test_confirmed_but_unsettled_is_kept(tmp_path: Path) -> None:
    f = _make(tmp_path, settled=False)
    _confirm(f)
    assert "unsettled=1" in _run(tmp_path)
    assert f.exists()


def test_not_in_sha256sums_is_kept_even_if_confirmed(tmp_path: Path) -> None:
    _make(tmp_path, "listed.dump")
    adhoc = _make(tmp_path, "pre-055-x.dump", listed=False)
    _confirm(adhoc)
    out = _run(tmp_path)
    assert adhoc.exists()
    assert "not_listed=1" in out


def test_unread_is_kept(tmp_path: Path) -> None:
    f = _make(tmp_path, atime_read=False)
    _run(tmp_path)
    assert f.exists()


def test_no_sha256sums_at_all_is_kept(tmp_path: Path) -> None:
    f = _make(tmp_path)
    (f.parent / "SHA256SUMS").unlink()
    _confirm(f)
    assert "no_sums=1" in _run(tmp_path)
    assert f.exists()


def test_nonempty_dir_not_removed(tmp_path: Path) -> None:
    f = _make(tmp_path)
    _run(tmp_path)
    assert f.parent.exists()
