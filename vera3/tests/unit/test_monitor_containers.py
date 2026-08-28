"""Монитор сверяет состав стека с compose, а не со списком имён внутри себя.

Прибитый список молчаливо расходится со стеком: 27.08.2026 `media-worker`
пропал на 16 часов, его в списке не было, и монитор всё это время отвечал
«все контейнеры подняты». Здесь проверяется, что теперь так не выйдет —
и, отдельно, что мёртвый docker даёт тревогу, а не пустой список и тишину.

Скрипт гоняется по-настоящему, с подставным `docker` в PATH: логика живёт
в bash, и проверять её пересказом на Python бессмысленно.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

MONITOR = Path(__file__).resolve().parents[2] / "scripts" / "vera3-monitor.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="нужны bash и jq",
)


def _stub_docker(tmp_path: Path, *, services: dict[str, int],
                 running: dict[str, int], config_ok: bool = True) -> Path:
    """Подставной docker: отвечает на `compose config` и `compose ps`.

    `services` — что объявлено в compose (имя → число реплик),
    `running` — сколько контейнеров реально живо.
    """
    spec = {"services": {
        name: ({"deploy": {"replicas": n}} if n != 1 else {})
        for name, n in services.items()
    }}
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "config.json").write_text(json.dumps(spec), encoding="utf-8")
    (bindir / "running.json").write_text(json.dumps(running), encoding="utf-8")

    script = f"""#!/bin/bash
here="{bindir.as_posix()}"
if [ "$1" = "compose" ] && [ "$2" = "config" ]; then
    {'cat "$here/config.json"' if config_ok else 'exit 1'}
    exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then
    svc="${{@: -1}}"
    n=$(jq -r --arg s "$svc" '.[$s] // 0' "$here/running.json")
    for i in $(seq 1 "$n"); do echo "container_${{svc}}_$i"; done
    exit 0
fi
exit 0
"""
    path = bindir / "docker"
    path.write_bytes(script.encode("utf-8"))
    path.chmod(0o755)
    return bindir


def _check(tmp_path: Path, **kw) -> tuple[int, str]:
    bindir = _stub_docker(tmp_path, **kw)
    env = dict(os.environ)
    env["PATH"] = f"{bindir.as_posix()}{os.pathsep}{env['PATH']}"
    env["COMPOSE_DIR"] = str(tmp_path)
    done = subprocess.run(
        ["bash", str(MONITOR), "--check-containers"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return done.returncode, done.stdout.strip()


class TestEverythingUp:
    def test_full_stack_is_quiet(self, tmp_path):
        code, out = _check(
            tmp_path,
            services={"gateway": 1, "media-worker": 1, "brain-triage": 5},
            running={"gateway": 1, "media-worker": 1, "brain-triage": 5},
        )
        assert code == 0
        assert out == ""


class TestMissingService:
    def test_service_without_containers_is_named(self, tmp_path):
        """Ровно случай 27.08: контейнера нет вовсе, а не остановлен."""
        code, out = _check(
            tmp_path,
            services={"gateway": 1, "media-worker": 1},
            running={"gateway": 1, "media-worker": 0},
        )
        assert code == 1
        assert "media-worker" in out
        assert "gateway" not in out

    def test_service_added_to_compose_is_watched_without_editing_the_script(self, tmp_path):
        """Новый сервис охраняется сразу — в этом вся суть замены списка."""
        code, out = _check(
            tmp_path,
            services={"gateway": 1, "ingestor-trello": 1},
            running={"gateway": 1, "ingestor-trello": 0},
        )
        assert code == 1 and "ingestor-trello" in out


class TestReplicas:
    def test_partial_replicas_are_a_problem(self, tmp_path):
        """Три живых из пяти — потеря 40% триажа, прежняя проверка молчала."""
        code, out = _check(
            tmp_path,
            services={"brain-triage": 5},
            running={"brain-triage": 3},
        )
        assert code == 1
        assert "3 из 5" in out

    def test_all_replicas_alive_is_quiet(self, tmp_path):
        code, out = _check(tmp_path, services={"brain-triage": 5},
                           running={"brain-triage": 5})
        assert code == 0 and out == ""


class TestSeveralAtOnce:
    def test_every_broken_service_is_named(self, tmp_path):
        """Авария 27.08 снесла ТРИ контейнера сразу, а не один.

        Вывод по строке на проблему — то, что вызывающий код склеивает в текст
        тревоги; если бы функция называла только первую, остальные пропали бы
        из сообщения так же тихо, как раньше пропадал весь media-worker."""
        code, out = _check(
            tmp_path,
            services={"gateway": 1, "media-worker": 1, "ingestor-trello": 1,
                      "bot-telegram": 1},
            running={"gateway": 1, "media-worker": 0, "ingestor-trello": 0,
                     "bot-telegram": 0},
        )
        assert code == 1
        assert len(out.splitlines()) == 3
        for name in ("media-worker", "ingestor-trello", "bot-telegram"):
            assert name in out
        assert "gateway" not in out


class TestBlindness:
    def test_missing_compose_dir_alerts_too(self, tmp_path):
        """Каталог пропал — тоже тревога, а не молчаливый пропуск сервисов."""
        bindir = _stub_docker(tmp_path, services={"gateway": 1}, running={"gateway": 1})
        env = dict(os.environ)
        env["PATH"] = f"{bindir.as_posix()}{os.pathsep}{env['PATH']}"
        env["COMPOSE_DIR"] = str(tmp_path / "нет-такого")
        done = subprocess.run(
            ["bash", str(MONITOR), "--check-containers"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert done.returncode == 1
        assert "нет каталога" in done.stdout

    def test_dead_docker_alerts_instead_of_saying_all_good(self, tmp_path):
        """Главная ловушка: пустой список — это не «нет проблем».

        Если бы функция молчала, монитор снял бы охрану со всего стека ровно
        тогда, когда докер лежит."""
        code, out = _check(
            tmp_path,
            services={"gateway": 1},
            running={"gateway": 1},
            config_ok=False,
        )
        assert code == 1
        assert "не читается" in out
