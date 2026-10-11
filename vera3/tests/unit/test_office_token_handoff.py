"""Функции office_token_handoff_lib.sh на синтетических env-файлах (без ssh/docker/prod)."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "office_token_handoff_lib.sh"
BASH = shutil.which("bash")
TOKEN = "S3cretTokenForTestsOnlyAbcdefghijklmnop0123"
OTHER = "AnotherTokenForTestsOnlyZyxwvutsrqponm98765"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash unavailable")


def _run(call: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    script = f'. "{LIB.as_posix()}"\n{call}\n'
    raw = subprocess.run(
        [BASH, "-c", script], input=stdin.encode(), capture_output=True, check=False
    )
    return subprocess.CompletedProcess(
        raw.args, raw.returncode, raw.stdout.decode(), raw.stderr.decode()
    )


def _env(tmp_path: Path, body: str) -> Path:
    p = tmp_path / ".env"
    p.write_bytes(body.encode())
    p.chmod(0o600)
    return p


def _upsert(p: Path, token: str = TOKEN) -> subprocess.CompletedProcess[str]:
    return _run(f'hv_upsert_registry "{p.as_posix()}" veranda_bot Veranda', token + "\n")


def _no_secret(r: subprocess.CompletedProcess[str]) -> None:
    assert TOKEN not in r.stdout + r.stderr
    assert OTHER not in r.stdout + r.stderr


def test_empty_registry_added(tmp_path: Path) -> None:
    p = _env(tmp_path, "A=1\nOFFICE_SERVICE_TOKENS=\nB=2\n")
    r = _upsert(p)
    assert r.returncode == 0, r.stderr
    assert p.read_text() == f"A=1\nOFFICE_SERVICE_TOKENS=veranda_bot:Veranda:{TOKEN}\nB=2\n"
    _no_secret(r)


def test_missing_key_appended(tmp_path: Path) -> None:
    p = _env(tmp_path, "A=1\n")
    assert _upsert(p).returncode == 0
    assert p.read_text() == f"A=1\nOFFICE_SERVICE_TOKENS=veranda_bot:Veranda:{TOKEN}\n"


def test_other_principals_and_lines_preserved(tmp_path: Path) -> None:
    body = (
        "# comment\nMCP_TOKENS=claude:ownertoken\nROOM_TOKENS=a:roomtoken\n"
        "OFFICE_SERVICE_TOKENS=svc:Proj:othertokenvalue,x:Y:zzz\nTAIL=last\n"
    )
    p = _env(tmp_path, body)
    r = _upsert(p)
    assert r.returncode == 0, r.stderr
    expected = body.replace(
        "x:Y:zzz\n", f"x:Y:zzz,veranda_bot:Veranda:{TOKEN}\n"
    )
    assert p.read_text() == expected
    _no_secret(r)


def test_same_token_is_noop(tmp_path: Path) -> None:
    body = f"OFFICE_SERVICE_TOKENS=veranda_bot:Veranda:{TOKEN}\n"
    p = _env(tmp_path, body)
    r = _upsert(p)
    assert r.returncode == 10
    assert p.read_text() == body
    assert not list(tmp_path.glob(".env.bak-*"))
    _no_secret(r)


def test_different_token_errors_and_file_unchanged(tmp_path: Path) -> None:
    body = f"OFFICE_SERVICE_TOKENS=veranda_bot:Veranda:{OTHER}\n"
    p = _env(tmp_path, body)
    r = _upsert(p)
    assert r.returncode == 1
    assert "refusing to overwrite" in r.stderr
    assert p.read_text() == body
    _no_secret(r)


def test_short_token_rejected(tmp_path: Path) -> None:
    p = _env(tmp_path, "A=1\n")
    r = _run(f'hv_upsert_registry "{p.as_posix()}" veranda_bot Veranda', "short\n")
    assert r.returncode == 1
    assert p.read_text() == "A=1\n"


@pytest.mark.parametrize("line", [f"MCP_TOKENS=claude:{TOKEN}", f"ROOM_TOKENS=a:{TOKEN},b:q"])
def test_collision_with_mcp_or_room_token_rejected(tmp_path: Path, line: str) -> None:
    body = f"{line}\n"
    p = _env(tmp_path, body)
    r = _upsert(p)
    assert r.returncode == 1
    assert "collides with MCP/ROOM tokens: yes" in r.stdout
    assert p.read_text() == body
    _no_secret(r)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_mode_0600_preserved_and_backup_made(tmp_path: Path) -> None:
    p = _env(tmp_path, "A=1\n")
    assert _upsert(p).returncode == 0
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    baks = list(tmp_path.glob(".env.bak-*"))
    assert len(baks) == 1 and baks[0].read_text() == "A=1\n"
    assert not list(tmp_path.glob(".hv.*"))


def test_retry_after_failure_reuses_token(tmp_path: Path) -> None:
    tokfile = tmp_path / "h" / "veranda_bot.token"
    r1 = _run(f'hv_gen_token "{tokfile.as_posix()}"')
    assert r1.returncode == 0, r1.stderr
    first = tokfile.read_text().strip()
    assert len(first) >= 32
    if sys.platform != "win32":
        assert stat.S_IMODE(tokfile.stat().st_mode) == 0o600
        assert stat.S_IMODE(tokfile.parent.stat().st_mode) == 0o700
    missing = tmp_path / "absent.env"
    bad = _run(f'hv_upsert_registry "{missing.as_posix()}" veranda_bot Veranda', first + "\n")
    assert bad.returncode == 1
    assert _run(f'hv_gen_token "{tokfile.as_posix()}"').returncode == 0
    assert tokfile.read_text().strip() == first
    p = _env(tmp_path, "OFFICE_SERVICE_TOKENS=\n")
    assert _upsert(p, first).returncode == 0
    assert first not in bad.stdout + bad.stderr + r1.stdout + r1.stderr


def test_set_kv_replaces_and_preserves(tmp_path: Path) -> None:
    p = _env(tmp_path, "X=1\nOFFICE_API_URL=old\nY=2\n")
    r = _run(f'hv_set_kv "{p.as_posix()}" OFFICE_API_URL', "https://dima.veranda.my\n")
    assert r.returncode == 0, r.stderr
    assert p.read_text() == "X=1\nOFFICE_API_URL=https://dima.veranda.my\nY=2\n"
    assert os.access(p, os.R_OK)
