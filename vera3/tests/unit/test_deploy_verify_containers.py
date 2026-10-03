"""deploy.sh verify_containers: разовые контейнеры `compose run` — не сервисы.

04.10.2026 идущий бэкфилл (`docker compose run … brain-triage`, образ прошлой
сборки) забраковал выкладку кодом 12, хотя все сервисы уже были на свежих
образах. Функция гоняется по-настоящему в bash, docker подменён.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "infra" / "deploy.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")

_STUB = r"""#!/bin/bash
# compose ps: сервис + разовый контейнер; inspect: метка oneoff и образы.
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then
  printf 'brain-triage\tvera3-brain-triage-1\trunning\timg\n'
  printf 'brain-triage\tvera3-brain-triage-run-abc\trunning\timg\n'
  exit 0
fi
args="$*"
case "$args" in
  *com.docker.compose.oneoff*vera3-brain-triage-run-abc*) echo True ;;
  *com.docker.compose.oneoff*) echo False ;;
  *"{{.Config.Image}}"*) echo vera3-brain-triage:latest ;;
  *"image inspect"*) echo sha256:NEW0000000000000000000000000 ;;
  *"{{.Image}}"*vera3-brain-triage-run-abc*) echo sha256:OLD0000000000000000000000000 ;;
  *"{{.Image}}"*) echo sha256:NEW0000000000000000000000000 ;;
  *Health*) echo healthy ;;
esac
exit 0
"""


def _run_verify(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_bytes(_STUB.encode())
    (bindir / "docker").chmod(0o755)
    infra = tmp_path / "infra"
    infra.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{bindir.as_posix()}{os.pathsep}{env['PATH']}"
    env["TARGET_DIR"] = tmp_path.as_posix()
    script = f'source "{DEPLOY.as_posix()}"; verify_containers'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=60)


def test_oneoff_run_container_on_old_image_does_not_fail_the_deploy(tmp_path):
    done = _run_verify(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "пропуск vera3-brain-triage-run-abc" in done.stdout
    assert "проверено контейнеров: 1, проблем: 0" in done.stdout


def test_sourcing_deploy_does_not_run_main(tmp_path):
    done = subprocess.run(["bash", "-c", f'source "{DEPLOY.as_posix()}"; echo sourced'],
                          capture_output=True, text=True, encoding="utf-8", timeout=60,
                          env={**os.environ, "TARGET_DIR": tmp_path.as_posix()})
    assert done.stdout.strip() == "sourced"
