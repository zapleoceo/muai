"""Volume is diagnostic; manifest and archive faults keep their own alerts."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

MONITOR = Path(__file__).resolve().parents[2] / "scripts" / "vera3-monitor.sh"



def _check_transitions(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    root = tmp_path / "backups"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    env_file = tmp_path / "env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=synthetic\nOWNER_TELEGRAM_ID=0\n")
    for name, body in {
        "docker": "exit 0", "logger": "exit 0",
        "curl": 'printf "%s\\n" "$*" >> "$CAPTURE"',
    }.items():
        fake = bindir / name
        fake.write_text("#!/bin/bash\n" + body + "\n")
        fake.chmod(0o700)
    tool = tmp_path / "retained.sh"
    tool.write_text('#!/bin/bash\nprintf "%s\\n" "$SCAN_OUTPUT"\nexit "${SCAN_EXIT:-0}"\n')
    sent = tmp_path / "sent"
    env = dict(os.environ, PATH=f"{bindir}:/usr/bin:/bin", ENV_FILE=str(env_file),
               STATE_DIR=str(state), BACKUP_ROOT=str(root), RETAINED_TOOL=str(tool),
               RETAINED_MANIFEST=str(root / "RETAINED.manifest"),
               RETAINED_CACHE=str(state / "cache"), CAPTURE=str(sent), RETAINED_REQUIRED="1")
    manifest = root / "RETAINED.manifest"

    def run(output=None, *, error=False):
        if output is None:
            manifest.unlink(missing_ok=True)
        else:
            manifest.write_text("synthetic")
        env["SCAN_OUTPUT"] = output or ""
        env["SCAN_EXIT"] = "1" if error else "0"
        sent.write_text("")
        for _ in range(3):
            result = subprocess.run(
                ["bash", str(MONITOR), "--check-backup-pending"], env=env,
                text=True, capture_output=True, timeout=15, check=True,
            )
            assert "backup_pending bytes=" in result.stdout
        return sent.read_text()

    def scan(size=0, changed=0, unverified=0, missing=0):
        return (f"status=ok other_bytes={size} retained_bytes=0 missing_files={missing} "
                f"changed_files={changed} unverified_files={unverified} cache=ok")

    assert not run(scan(10 * 1073741824))
    (state / "backup_pending").write_text("0")
    assert not run(scan())
    assert not (state / "backup_pending").exists()
    assert "unreadable/invalid" in run()
    assert "readable and valid again" in run(scan(10 * 1073741824))
    assert "unreadable/invalid" in run("invalid synthetic manifest", error=True)
    assert "readable and valid again" in run(scan())
    assert "changed/unverified" in run(scan(changed=1))
    run(scan())
    assert "changed/unverified" in run(scan(unverified=1))
    assert "missing on disk" in run(scan(missing=1))
    assert "no missing files" in run(scan())
    run("invalid", error=True)
    env["RETAINED_REQUIRED"] = "0"
    assert not run()
    assert (state / "backup_manifest").exists()


@unittest.skipIf(shutil.which("bash") is None, "requires bash")
class BackupNoiseTests(unittest.TestCase):
    def test_volume_and_manifest_transitions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vera-monitor-synthetic-") as directory:
            _check_transitions(Path(directory))


if __name__ == "__main__":
    unittest.main()
