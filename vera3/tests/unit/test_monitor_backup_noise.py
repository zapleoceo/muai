"""Backup size alone is not a fault; real disk pressure must still alert."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

MONITOR = Path(__file__).resolve().parents[2] / "scripts" / "vera3-monitor.sh"


@unittest.skipIf(shutil.which("bash") is None, "requires bash")
class BackupNoiseTests(unittest.TestCase):
    def run_disk_check(self, percent: int, runway: int = 30) -> list[str]:
        source = MONITOR.read_text(encoding="utf-8")
        disk = source.split("# ─── 3. Диск", 1)[1].split("# ─── 3b.", 1)[0]
        disk = disk.split("\n", 1)[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "kept.dump"
            with backup.open("wb") as stream:
                stream.truncate(4 * 1073741824)
            script = (
                """
set -eu
df() {
    echo 'Filesystem 1K-blocks Used Available Use% Mounted'
    echo "/dev/test 100000000 73000000 27000000 ${TEST_DISK_PERCENT}% /"
}
project_days() { echo "$TEST_RUNWAY"; }
alert() { echo "ALERT $1"; }
recover() { :; }
"""
                + disk
            )
            result = subprocess.run(
                ["bash"],
                input=script,
                capture_output=True,
                text=True,
                timeout=15,
                env=dict(
                    os.environ,
                    STATE_DIR=str(root / "state"),
                    BACKUP_ROOT=str(root),
                    TEST_DISK_PERCENT=str(percent),
                    TEST_RUNWAY=str(runway),
                    DISK_RUNWAY_ALERT_DAYS="7",
                ),
                check=True,
            )
            self.assertEqual(backup.stat().st_size, 4 * 1073741824)
            return [
                line for line in result.stdout.splitlines() if line.startswith("ALERT ")
            ]

    def test_four_gb_of_backups_on_healthy_disk_are_silent(self) -> None:
        self.assertEqual(self.run_disk_check(73), [])

    def test_real_disk_warning_remains(self) -> None:
        self.assertEqual(self.run_disk_check(86), ["ALERT disk_warn"])

    def test_critical_disk_pressure_remains(self) -> None:
        self.assertEqual(self.run_disk_check(93), ["ALERT disk_critical"])

    def test_short_disk_runway_remains(self) -> None:
        self.assertEqual(self.run_disk_check(73, runway=3), ["ALERT disk_runway"])


if __name__ == "__main__":
    unittest.main()
