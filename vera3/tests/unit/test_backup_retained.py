"""Удерживаемый архив бэкапов: манифест, ротация vera-backup.sh и монитор.

Скрипты гоняются по-настоящему на временном дереве (DEST/BACKUP_ROOT/STATE_DIR
в tmp_path, подставные docker/curl/logger); реальные пути сервера не затрагиваются.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
BACKUP = SCRIPTS / "vera-backup.sh"
MONITOR = SCRIPTS / "vera3-monitor.sh"
TOOL = SCRIPTS / "vera-retained-manifest.sh"


def _gnu_ok() -> bool:
    if shutil.which("bash") is None:
        return False
    r = subprocess.run(
        ["bash", "-c", "find --version | grep -q GNU && stat -c %s / >/dev/null && t=$(mktemp) && touch -d '2020-01-01' \"$t\" && rm -f \"$t\""],
        capture_output=True,
        check=False,
    )
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _gnu_ok(), reason="нужен bash и GNU findutils/coreutils")

OLD = "2026-10-21"
HELD_FILES = (f"aibroker/daily/{OLD}/aibroker.dump", f"aibroker/daily/{OLD}/secrets.tar.gz")


def _sh(cmd: list[str], env: dict[str, str] | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, env=env, check=check)


def _uid(path: Path) -> str:
    return _sh(["bash", "-c", f'stat -c %u "{path.as_posix()}"'], check=True).stdout.strip()


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    sent = tmp / "sent.txt"
    logs = tmp / "log.txt"
    for name, body in (
        ("curl", f'echo "$@" >> "{sent.as_posix()}"\nexit 0\n'),
        ("logger", f'echo "$@" >> "{logs.as_posix()}"\nexit 0\n'),
        ("docker", 'if [ "$1" = ps ]; then exit 0; fi\nexit 1\n'),
    ):
        path = bindir / name
        path.write_bytes(f"#!/bin/bash\n{body}".encode())
        path.chmod(0o755)
    if shutil.which("flock") is None:  # git-bash без util-linux: заглушка, блокировки здесь не проверяются
        shim = bindir / "flock"
        shim.write_bytes(b"#!/bin/bash\nexit 0\n")
        shim.chmod(0o755)
    envf = tmp / "env"
    envf.write_text("TELEGRAM_BOT_TOKEN=t\nOWNER_TELEGRAM_ID=1\n", encoding="utf-8")
    env = dict(
        os.environ,
        PATH=f"{bindir.as_posix()}{os.pathsep}{os.environ['PATH']}",
        ENV_FILE=str(envf),
        STATE_DIR=str(tmp / "state"),
        RETAINED_CACHE=str(tmp / "state" / "retained.verified"),
        RETAINED_LOCK=str(tmp / "retained.lock"),
        BACKUP_ROOT=str(tmp / "root"),
        DEST=str(tmp / "root"),
        WWW_ROOT=str(tmp / "www"),
        RETAINED_TOOL=str(TOOL),
        RETAINED_TRUST_UID=_uid(tmp),
        BACKUP_GROUP="root",
        BACKUP_PENDING_ALERT_GB="0",  # любой неучтённый байт — за порогом
    )
    env.pop("SKIP_DB_PROJECTS", None)
    env.pop("RETAINED_MANIFEST", None)
    env.pop("RETAINED_REQUIRED", None)
    env["HOLD_DATES"] = ""  # проверяем именно манифест; страховка HOLD_DATES — отдельным тестом
    env.update(extra)
    return env


def _file(root: Path, rel: str, data: bytes = b"data", when: str | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if when:
        _sh(["touch", "-d", when, str(p)], check=True)
    return p


def _age(path: Path, when: str = "2026-01-01") -> None:
    _sh(["touch", "-d", when, str(path)], check=True)


def _archive(root: Path) -> Path:
    """Дерево с удерживаемым архивом + манифест, собранный утилитой."""
    for rel in HELD_FILES:
        _file(root, rel, rel.encode(), "2026-10-21 03:00")
    _age(root / "aibroker" / "daily" / OLD)
    lst = root.parent / "paths.txt"
    lst.write_text("# архив\n" + "\n".join(HELD_FILES) + "\n", encoding="utf-8")
    return lst


def _generate(tmp: Path, env: dict[str, str], lst: Path) -> subprocess.CompletedProcess[str]:
    return _sh(["bash", str(TOOL), "generate", str(lst)], env=env)


def _verified(tmp: Path, env: dict[str, str], lst: Path) -> None:
    """Манифест + полный verify (кэш): штатное состояние после установки."""
    root = tmp / "root"
    assert _generate(tmp, env, lst).returncode == 0
    v = _sh(["bash", str(TOOL), "verify"], env=env)
    assert v.returncode == 0, v.stdout + v.stderr
    assert (tmp / "state" / "retained.verified").exists()
    assert root.exists()


def _monitor(tmp: Path, env: dict[str, str], runs: int = 2) -> tuple[str, str]:
    """Два прогона (FAIL_STREAK=2) → stdout последнего и тексты, ушедшие в TG."""
    out = ""
    for _ in range(runs):
        r = _sh(["bash", str(MONITOR), "--check-backup-pending"], env=env, check=True)
        out = r.stdout
    sent = tmp / "sent.txt"
    return out, sent.read_text(encoding="utf-8") if sent.exists() else ""


# ─── утилита манифеста ──────────────────────────────────────────────────────

def test_generate_validate_verify_roundtrip(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    lst = _archive(root)
    r = _generate(tmp_path, env, lst)
    assert r.returncode == 0, r.stderr
    text = (root / "RETAINED.manifest").read_text(encoding="utf-8")
    assert text.startswith("# vera-retained-manifest v1\n")
    assert text.rstrip().endswith("# end entries=2")
    first = next(line for line in text.splitlines() if not line.startswith("#")).split("\t")
    assert len(first[0]) == 64 and first[3] == HELD_FILES[0]
    assert _sh(["bash", str(TOOL), "validate"], env=env).returncode == 0
    v = _sh(["bash", str(TOOL), "verify"], env=env)
    assert v.returncode == 0 and "OK   " + HELD_FILES[0] in v.stdout


def test_verify_detects_tampering_with_same_size(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    _generate(tmp_path, env, _archive(root))
    victim = root / HELD_FILES[0]
    victim.write_bytes(b"X" * victim.stat().st_size)
    _sh(["touch", "-d", "2026-10-21 03:00", str(victim)], check=True)
    v = _sh(["bash", str(TOOL), "verify"], env=env)
    assert v.returncode == 1 and "sha256 не совпал" in v.stdout


def test_generate_refuses_missing_and_bad_paths_writes_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    lst = _archive(root)
    lst.write_text(f"{HELD_FILES[0]}\nnope/absent.dump\n../etc/passwd\n/abs\n", encoding="utf-8")
    r = _generate(tmp_path, env, lst)
    assert r.returncode == 1
    assert "НЕТ ОБЫЧНОГО ФАЙЛА: nope/absent.dump" in r.stderr and "ПЛОХОЙ ПУТЬ" in r.stderr
    assert not (root / "RETAINED.manifest").exists()


def test_generate_does_not_overwrite_without_force(tmp_path: Path) -> None:
    env = _env(tmp_path)
    lst = _archive(tmp_path / "root")
    assert _generate(tmp_path, env, lst).returncode == 0
    assert _generate(tmp_path, env, lst).returncode == 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace("# vera-retained-manifest v1", "# v0"),
        lambda t: t.replace("# end entries=2", "# end entries=3"),
        lambda t: "\n".join(t.splitlines()[:-1]) + "\n",  # усечён: нет хвоста
        lambda t: t + "garbage\n",
        lambda t: t.replace("\t", " "),
        lambda t: t.replace(HELD_FILES[0], "../" + HELD_FILES[0]),
        lambda t: t.replace("# end entries=2\n", "") + "".join(t.splitlines(True)[2:3]) + "# end entries=3\n",  # дубль
    ],
)
def test_validate_rejects_garbled(tmp_path: Path, mutate) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    _generate(tmp_path, env, _archive(root))
    m = root / "RETAINED.manifest"
    text = m.read_text(encoding="utf-8")
    m.chmod(0o644)
    m.write_bytes(mutate(text).encode("utf-8"))
    r = _sh(["bash", str(TOOL), "validate"], env=env)
    assert r.returncode == 2, r.stdout + r.stderr


def test_validate_rejects_wrong_owner(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _generate(tmp_path, env, _archive(tmp_path / "root"))
    env["RETAINED_TRUST_UID"] = "424242"
    r = _sh(["bash", str(TOOL), "validate"], env=env)
    assert r.returncode == 2 and "принадлежит uid" in r.stderr


def test_holds_matches_directory_prefix_only(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _generate(tmp_path, env, _archive(tmp_path / "root"))
    held = _sh(["bash", str(TOOL), "holds", f"aibroker/daily/{OLD}"], env=env)
    other = _sh(["bash", str(TOOL), "holds", "aibroker/daily/2026-10-2"], env=env)  # префикс имени — не каталог
    assert held.returncode == 0 and other.returncode == 1


# ─── монитор ────────────────────────────────────────────────────────────────

def test_monitor_unchanged_archive_no_alert_and_reports_retained(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    _verified(tmp_path, env, _archive(root))
    out, sent = _monitor(tmp_path, env)
    assert "bytes=0 " in out and "manifest=ok" in out
    retained = sum((root / rel).stat().st_size for rel in HELD_FILES)
    assert f"retained_bytes={retained} " in out
    assert sent == ""


def test_monitor_new_file_is_counted(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    _verified(tmp_path, env, _archive(root))
    _file(root, "vera/daily/2026-10-25/secrets.tar.gz", b"12345")
    out, sent = _monitor(tmp_path, env)
    assert "bytes=5 " in out and "Backups on disk" in sent


@pytest.mark.parametrize("how", ["grow", "touch"])
def test_monitor_modified_archive_file_is_counted(tmp_path: Path, how: str) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    _verified(tmp_path, env, _archive(root))
    victim = root / HELD_FILES[0]
    size = victim.stat().st_size
    if how == "grow":
        victim.write_bytes(victim.read_bytes() + b"+more")
        _age(victim, "2026-10-21 03:00")
    else:
        _age(victim, "2026-10-22 09:00")  # тот же размер, другой mtime
    out, sent = _monitor(tmp_path, env)
    assert "changed_files=1" in out and "Backups on disk" in sent
    assert f"bytes={size + (5 if how == 'grow' else 0)} " in out


def test_monitor_garbled_manifest_alerts_unknown_not_healthy(tmp_path: Path) -> None:
    env = _env(tmp_path, BACKUP_PENDING_ALERT_GB="3")  # объём мал — но «неизвестно» всё равно алерт
    root = tmp_path / "root"
    _archive(root)
    (root / "RETAINED.manifest").write_text("мусор\n", encoding="utf-8")
    out, sent = _monitor(tmp_path, env)
    assert "manifest=error" in out and "retained_bytes=0 " in out
    assert "unreadable/invalid" in sent and "UNKNOWN" in sent


def test_monitor_missing_archive_file_alerts(tmp_path: Path) -> None:
    env = _env(tmp_path, BACKUP_PENDING_ALERT_GB="3")
    root = tmp_path / "root"
    _verified(tmp_path, env, _archive(root))
    (root / HELD_FILES[1]).unlink()
    out, sent = _monitor(tmp_path, env)
    assert "missing_files=1" in out and "missing on disk" in sent


def test_monitor_without_manifest_old_behaviour_only_when_not_required(tmp_path: Path) -> None:
    env = _env(tmp_path, RETAINED_REQUIRED="0")
    root = tmp_path / "root"
    _archive(root)
    out, sent = _monitor(tmp_path, env)
    assert "manifest=none" in out and "retained_bytes=0 " in out
    total = sum((root / rel).stat().st_size for rel in HELD_FILES)
    assert f"bytes={total} " in out and "Backups on disk" in sent


def test_monitor_missing_manifest_in_protected_mode_is_error(tmp_path: Path) -> None:
    env = _env(tmp_path, BACKUP_PENDING_ALERT_GB="3")  # дефолт RETAINED_REQUIRED=1
    _archive(tmp_path / "root")
    out, sent = _monitor(tmp_path, env)
    assert "manifest=error" in out
    assert "UNKNOWN" in sent and "manifest MISSING" in sent


def test_monitor_reports_retained_file_changed_explicitly_with_path(tmp_path: Path) -> None:
    env = _env(tmp_path, BACKUP_PENDING_ALERT_GB="3")
    root = tmp_path / "root"
    _verified(tmp_path, env, _archive(root))
    _age(root / HELD_FILES[0], "2026-10-22 09:00")
    out, sent = _monitor(tmp_path, env)
    assert "changed_files=1" in out
    assert "Retained file changed" in sent and HELD_FILES[0] in sent


def test_monitor_changed_alert_is_throttled(tmp_path: Path) -> None:
    env = _env(tmp_path, BACKUP_PENDING_ALERT_GB="3")
    root = tmp_path / "root"
    _verified(tmp_path, env, _archive(root))
    _age(root / HELD_FILES[0], "2026-10-22 09:00")
    _, sent = _monitor(tmp_path, env, runs=6)
    assert sent.count("Retained file changed") == 1


# ─── ротация vera-backup.sh ─────────────────────────────────────────────────

def _rotation_tree(root: Path) -> None:
    _archive(root)
    _file(root, "aibroker/daily/2026-09-01/secrets.tar.gz", b"old")
    _file(root, "stepan2/daily/2026-09-02/secrets.tar.gz", b"old")
    _file(root, "vera/weekly/2026-08-30/vera.dump", b"old")
    _file(root, "vera/daily/2026-10-24/secrets.tar.gz", b"fresh", f"{time.strftime('%Y-%m-%d')} 00:00")
    for d in ("aibroker/daily/2026-09-01", "stepan2/daily/2026-09-02", "vera/weekly/2026-08-30"):
        _age(root / d)


def _rotate(tmp: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    www = tmp / "www"
    for rel in ("aibroker/.env", "stepan2/infra/.env", "vera3/infra/.env"):
        _file(www, rel, b"TOKEN_SECRET=x\n")
    return _sh(["bash", str(BACKUP)], env=env, check=True)


def test_rotation_keeps_held_dirs_removes_unheld(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    _rotation_tree(root)
    assert _generate(tmp_path, env, tmp_path / "paths.txt").returncode == 0
    r = _rotate(tmp_path, env)
    assert (root / HELD_FILES[0]).exists() and (root / HELD_FILES[1]).exists()
    assert "удерживаю" in r.stdout
    for gone in ("aibroker/daily/2026-09-01", "stepan2/daily/2026-09-02", "vera/weekly/2026-08-30"):
        assert not (root / gone).exists(), gone
    assert (root / "vera/daily/2026-10-24").exists()


def test_rotation_garbled_manifest_removes_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    _rotation_tree(root)
    (root / "RETAINED.manifest").write_text("# vera-retained-manifest v1\nbroken\n", encoding="utf-8")
    r = _rotate(tmp_path, env)
    assert "ROTATION SKIPPED" in r.stdout
    for keep in ("aibroker/daily/2026-09-01", "stepan2/daily/2026-09-02", "vera/weekly/2026-08-30"):
        assert (root / keep).exists(), keep


def test_rotation_missing_tool_with_manifest_removes_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path, RETAINED_TOOL=str(tmp_path / "absent.sh"))
    root = tmp_path / "root"
    _rotation_tree(root)
    (root / "RETAINED.manifest").write_text("x\n", encoding="utf-8")
    r = _rotate(tmp_path, env)
    assert "ROTATION SKIPPED" in r.stdout
    assert (root / "aibroker/daily/2026-09-01").exists()


def test_rotation_manifest_removed_after_install_deletes_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)  # защищённый режим по умолчанию
    root = tmp_path / "root"
    _rotation_tree(root)
    assert _generate(tmp_path, env, tmp_path / "paths.txt").returncode == 0
    (root / "RETAINED.manifest").chmod(0o644)
    (root / "RETAINED.manifest").unlink()  # потерян/не поставлен после успешной установки
    r = _rotate(tmp_path, env)
    assert "ERROR" in r.stdout and "ОТСУТСТВУЕТ" in r.stdout and "ROTATION SKIPPED" in r.stdout
    for keep in (*HELD_FILES, "aibroker/daily/2026-09-01/secrets.tar.gz", "vera/weekly/2026-08-30/vera.dump"):
        assert (root / keep).exists(), keep


def test_rotation_without_manifest_is_as_before_only_when_not_required(tmp_path: Path) -> None:
    env = _env(tmp_path, RETAINED_REQUIRED="0")
    root = tmp_path / "root"
    _rotation_tree(root)
    r = _rotate(tmp_path, env)
    assert "ротация без защиты" in r.stdout
    assert not (root / "aibroker/daily/2026-09-01").exists()
    assert not (root / f"aibroker/daily/{OLD}").exists()


def test_rotation_hold_dates_fallback_protects_without_manifest_in_unprotected_mode(tmp_path: Path) -> None:
    env = _env(tmp_path, RETAINED_REQUIRED="0", HOLD_DATES=OLD)
    root = tmp_path / "root"
    _rotation_tree(root)
    _rotate(tmp_path, env)
    assert (root / HELD_FILES[0]).exists()
    assert not (root / "aibroker/daily/2026-09-01").exists()


def test_rotation_hold_dates_adds_to_valid_manifest(tmp_path: Path) -> None:
    env = _env(tmp_path, HOLD_DATES="2026-09-01")
    root = tmp_path / "root"
    _rotation_tree(root)
    assert _generate(tmp_path, env, tmp_path / "paths.txt").returncode == 0
    _rotate(tmp_path, env)
    assert (root / "aibroker/daily/2026-09-01").exists() and (root / HELD_FILES[0]).exists()
    assert not (root / "stepan2/daily/2026-09-02").exists()


def test_default_hold_dates_present_in_script() -> None:
    assert "2026-10-18 2026-10-21 2026-10-22 2026-10-23" in BACKUP.read_text(encoding="utf-8")


# ─── целостность: идентичность из кэша verify ───────────────────────────────

def test_monitor_same_size_restored_mtime_is_detected(tmp_path: Path) -> None:
    env = _env(tmp_path, BACKUP_PENDING_ALERT_GB="3")
    root = tmp_path / "root"
    _verified(tmp_path, env, _archive(root))
    victim = root / HELD_FILES[0]
    victim.write_bytes(b"X" * victim.stat().st_size)  # тот же размер, другое содержимое
    _age(victim, "2026-10-21 03:00")  # mtime возвращён; ctime вернуть нельзя
    out, sent = _monitor(tmp_path, env)
    assert "unverified_files=1" in out and "retained_bytes=" in out
    assert "Retained file changed/unverified" in sent and HELD_FILES[0] in sent


def test_monitor_never_verified_archive_is_counted_as_pending(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    assert _generate(tmp_path, env, _archive(root)).returncode == 0  # verify не запускали
    total = sum((root / rel).stat().st_size for rel in HELD_FILES)
    out, sent = _monitor(tmp_path, env)
    assert f"bytes={total} " in out and "retained_bytes=0 " in out and "unverified_files=2" in out
    assert "not verified by full sha256" in sent


def test_monitor_stale_cache_header_means_unverified(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _verified(tmp_path, env, _archive(tmp_path / "root"))
    cache = tmp_path / "state" / "retained.verified"
    cache.write_text("# vera-retained-cache v1 manifest=" + "0" * 64 + "\n" + "".join(cache.read_text().splitlines(True)[1:]))
    out, _ = _monitor(tmp_path, env)
    assert "unverified_files=2" in out and "cache=stale" in out


def test_verify_failure_is_not_cached(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    _generate(tmp_path, env, _archive(root))
    victim = root / HELD_FILES[0]
    victim.write_bytes(b"X" * victim.stat().st_size)
    _age(victim, "2026-10-21 03:00")
    assert _sh(["bash", str(TOOL), "verify"], env=env).returncode == 1
    cached = (tmp_path / "state" / "retained.verified").read_text()
    assert HELD_FILES[0] not in cached and HELD_FILES[1] in cached


# ─── симлинки ───────────────────────────────────────────────────────────────

def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("симлинки недоступны на этой платформе/правах")


def test_generate_rejects_symlinked_parent_dir(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    outside = tmp_path / "outside" / "d"
    _file(outside.parent, "d/x.dump", b"escape")
    _symlink_or_skip(root / "linked", outside.parent)
    lst = tmp_path / "paths.txt"
    lst.write_text("linked/d/x.dump\n", encoding="utf-8")
    r = _generate(tmp_path, env, lst)
    assert r.returncode == 1 and "СИМЛИНК В ПУТИ: linked/d/x.dump" in r.stderr
    assert not (root / "RETAINED.manifest").exists()


def test_generate_rejects_symlinked_file(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    real = _file(tmp_path / "outside", "real.dump", b"x")
    (root / "a").mkdir(parents=True)
    _symlink_or_skip(root / "a" / "l.dump", real)
    lst = tmp_path / "paths.txt"
    lst.write_text("a/l.dump\n", encoding="utf-8")
    r = _generate(tmp_path, env, lst)
    assert r.returncode == 1 and "a/l.dump" in r.stderr


def test_manifest_with_symlinked_parent_is_invalid_everywhere(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    _generate(tmp_path, env, _archive(root))
    # после установки каталог подменили симлинком наружу
    moved = tmp_path / "moved"
    (root / "aibroker").rename(moved)
    _symlink_or_skip(root / "aibroker", moved)
    for args in (["validate"], ["verify"], ["holds", f"aibroker/daily/{OLD}"], ["pending"]):
        r = _sh(["bash", str(TOOL), *args], env=env)
        assert r.returncode == 2 and "симлинк в пути" in r.stderr, (args, r.stdout, r.stderr)


# ─── гонка ротации и блокировка ─────────────────────────────────────────────

def _alt_manifest(tmp: Path, env: dict[str, str]) -> Path:
    """Манифест, который (в отличие от исходного) держит ещё и старый каталог X."""
    alt_list = tmp / "paths-alt.txt"
    alt_list.write_text("\n".join((*HELD_FILES, "aibroker/daily/2026-09-01/secrets.tar.gz")) + "\n", encoding="utf-8")
    alt = tmp / "root" / "RETAINED.alt"
    r = _sh(["bash", str(TOOL), "generate", str(alt_list), "--out", str(alt)], env=env)
    assert r.returncode == 0, r.stderr
    return alt


@pytest.mark.parametrize("swap", ["replace", "remove"])
def test_rotation_race_manifest_changes_between_decision_and_rm(tmp_path: Path, swap: str) -> None:
    root = tmp_path / "root"
    _rotation_tree(root)
    env = _env(tmp_path)
    assert _generate(tmp_path, env, tmp_path / "paths.txt").returncode == 0
    manifest = root / "RETAINED.manifest"
    if swap == "replace":
        alt = _alt_manifest(tmp_path, env)
        env["ROTATE_RACE_HOOK"] = f'mv -f "{alt.as_posix()}" "{manifest.as_posix()}"'
    else:
        env["ROTATE_RACE_HOOK"] = f'rm -f "{manifest.as_posix()}"'
    r = _rotate(tmp_path, env)
    assert "манифест изменился/пропал после решения" in r.stdout
    for keep in (*HELD_FILES, "aibroker/daily/2026-09-01/secrets.tar.gz", "stepan2/daily/2026-09-02/secrets.tar.gz"):
        assert (root / keep).exists(), keep


@pytest.mark.skipif(shutil.which("flock") is None, reason="нужен flock (util-linux)")
def test_rotation_skipped_when_lock_is_held(tmp_path: Path) -> None:
    env = _env(tmp_path, RETAINED_LOCK_WAIT="1")
    root = tmp_path / "root"
    _rotation_tree(root)
    assert _generate(tmp_path, env, tmp_path / "paths.txt").returncode == 0
    holder = subprocess.Popen(["bash", "-c", f'exec 9>"{env["RETAINED_LOCK"]}"; flock -x 9; sleep 20'], env=env)
    try:
        time.sleep(1)
        r = _rotate(tmp_path, env)
    finally:
        holder.kill()
    assert "не взята блокировка" in r.stdout
    assert (root / "aibroker/daily/2026-09-01").exists()


def test_install_validates_then_moves_atomically(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = tmp_path / "root"
    lst = _archive(root)
    new = root / "RETAINED.manifest.new"
    assert _sh(["bash", str(TOOL), "generate", str(lst), "--out", str(new)], env=env).returncode == 0
    r = _sh(["bash", str(TOOL), "install", str(new)], env=env)
    assert r.returncode == 0 and (root / "RETAINED.manifest").exists() and not new.exists(), r.stderr
    bad = root / "RETAINED.manifest.new"
    bad.write_text("мусор\n", encoding="utf-8")
    assert _sh(["bash", str(TOOL), "install", str(bad)], env=env).returncode == 2
    assert _sh(["bash", str(TOOL), "validate"], env=env).returncode == 0  # прежний манифест цел


# ─── vera-backup-prune.sh: файл из манифеста не удаляется ───────────────────

PRUNE = SCRIPTS / "vera-backup-prune.sh"


def _prune_setup(tmp: Path) -> tuple[Path, str]:
    """Удаляемый по старым правилам дамп: SHA256SUMS + .nas-confirmed + старый mtime."""
    import hashlib

    root = tmp / "root"
    f = _file(root, f"aibroker/daily/{OLD}/aibroker.dump", b"payload")
    sha = hashlib.sha256(b"payload").hexdigest()
    (f.parent / "SHA256SUMS").write_text(f"{sha}  ./aibroker.dump\n", encoding="utf-8")
    Path(str(f) + ".nas-confirmed").write_text(sha + "\n", encoding="utf-8")
    _age(f, "2026-01-01")
    return f, sha


def _prune(tmp: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    fuser = tmp / "fake-fuser"
    fuser.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    fuser.chmod(0o755)
    env = dict(env, ROOT=env["BACKUP_ROOT"], FUSER=str(fuser), SETTLE_MIN="1")
    return _sh(["bash", str(PRUNE)], env=env, check=True)


def test_prune_control_deletes_confirmed_file_without_manifest(tmp_path: Path) -> None:
    f, _ = _prune_setup(tmp_path)
    _prune(tmp_path, _env(tmp_path))
    assert not f.exists()  # контроль: без манифеста поведение прежнее


def test_prune_never_deletes_manifest_listed_file_even_if_confirmed(tmp_path: Path) -> None:
    f, _ = _prune_setup(tmp_path)
    env = _env(tmp_path)
    lst = tmp_path / "paths.txt"
    lst.write_text(f"aibroker/daily/{OLD}/aibroker.dump\n", encoding="utf-8")
    assert _generate(tmp_path, env, lst).returncode == 0
    r = _prune(tmp_path, env)
    assert f.exists() and "retained=1" in r.stdout


def test_prune_invalid_manifest_deletes_nothing(tmp_path: Path) -> None:
    f, _ = _prune_setup(tmp_path)
    (tmp_path / "root" / "RETAINED.manifest").write_text("мусор\n", encoding="utf-8")
    r = _prune(tmp_path, _env(tmp_path))
    assert f.exists() and "PRUNE SKIPPED" in r.stdout
