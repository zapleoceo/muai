"""Монитор замечает, что учёт миграций разошёлся с репозиторием.

13.09.2026 в schema_migrations последней была 025, а файлов было до 030:
026 и 027 накатаны руками без записи, 028 и 029 не накатаны вовсе, 030
накатана наполовину (расширение vector есть, колонки нет). Ни деплой, ни
монитор этого не видели. Здесь это состояние воспроизводится на подставном
docker/psql, и проверяется, что каждая из пяти названа правильным словом.

Скрипт гоняется по-настоящему: логика в bash (vera3-monitor.sh
--check-migrations → scripts/check_migrations.sh).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

MONITOR = Path(__file__).resolve().parents[2] / "scripts" / "vera3-monitor.sh"
REAL_MIGRATIONS = Path(__file__).resolve().parents[2] / "infra" / "migrations"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")

# Подставной docker: последний аргумент `docker exec … psql -tAc <sql>` — это SQL.
# Выражение-проба засчитывается, если все имена в кавычках из него есть в
# existing.txt (для to_regclass префикс public. срезается).
_STUB = r"""#!/bin/bash
here="__HERE__"
sql="${@: -1}"
[ -f "$here/db_down" ] && exit 2
case "$sql" in
  *"to_regclass('public.schema_migrations')"*) echo t ;;
  "SELECT version FROM schema_migrations") cat "$here/applied.txt" ;;
  "SELECT ("*)
    sum=0
    while IFS= read -r expr; do
      ok=1
      for name in $(grep -oE "'[^']+'" <<< "$expr" | tr -d "'" | sed 's/^public\.//'); do
        grep -qxF "$name" "$here/existing.txt" || ok=0
      done
      sum=$(( sum + ok ))
    done < <(sed 's/^SELECT //; s/)::int+/)::int\n/g' <<< "$sql")
    echo "$sum" ;;
esac
exit 0
"""


def _run(tmp_path: Path, *, applied: list[str], existing: list[str],
         files: list[str], db_down: bool = False) -> tuple[int, list[str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    # Байтами: на Windows write_text дал бы CRLF, а psql печатает LF.
    (bindir / "applied.txt").write_bytes(("\n".join(applied) + "\n").encode())
    (bindir / "existing.txt").write_bytes(("\n".join(existing) + "\n").encode())
    if db_down:
        (bindir / "db_down").write_text("", encoding="utf-8")
    stub = bindir / "docker"
    stub.write_bytes(_STUB.replace("__HERE__", bindir.as_posix()).encode("utf-8"))
    stub.chmod(0o755)

    mig = tmp_path / "migrations"
    mig.mkdir()
    for name in files:
        shutil.copy(REAL_MIGRATIONS / f"{name}.sql", mig / f"{name}.sql")

    env = dict(os.environ)
    env["PATH"] = f"{bindir.as_posix()}{os.pathsep}{env['PATH']}"
    env["MIGRATIONS_DIR"] = str(mig)
    done = subprocess.run(["bash", str(MONITOR), "--check-migrations"],
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=60)
    return done.returncode, done.stdout.strip().splitlines()


PROD_0913_FILES = [
    "024_slack", "025_slack_auth", "026_claude_session_queue",
    "027_events_chat_sent_index", "028_memberships_child_index",
    "029_usage_log_created_at_index", "030_event_embeddings_pgvector",
]


def test_state_of_2026_09_13_is_named_migration_by_migration(tmp_path):
    code, lines = _run(
        tmp_path,
        applied=["024_slack", "025_slack_auth"],
        existing=["claude_session_queue", "ix_claude_queue_status",
                  "ix_events_tg_chat_sent", "vector", "memberships", "usage_log"],
        files=PROD_0913_FILES,
    )
    assert code == 1
    by_version = {line.split(":", 1)[0]: line for line in lines}
    assert set(by_version) == {
        "026_claude_session_queue", "027_events_chat_sent_index",
        "028_memberships_child_index", "029_usage_log_created_at_index",
        "030_event_embeddings_pgvector",
    }
    assert "объекты есть (2 из 2)" in by_version["026_claude_session_queue"]
    assert "объекты есть (1 из 1)" in by_version["027_events_chat_sent_index"]
    assert "не применена" in by_version["028_memberships_child_index"]
    assert "не применена" in by_version["029_usage_log_created_at_index"]
    assert "частично" in by_version["030_event_embeddings_pgvector"]
    assert "1 из 2" in by_version["030_event_embeddings_pgvector"]


def test_everything_recorded_is_quiet(tmp_path):
    code, lines = _run(tmp_path, applied=PROD_0913_FILES, existing=[],
                       files=PROD_0913_FILES)
    assert code == 0
    assert lines == []


def test_recorded_version_without_file_is_reported(tmp_path):
    """Переименованный или удалённый файл — тоже расхождение, а не тишина."""
    code, lines = _run(tmp_path, applied=["025_slack_auth", "026_old_name"],
                       existing=[], files=["025_slack_auth"])
    assert code == 1
    assert lines == ["026_old_name: записана в учёт, но файла в репозитории нет"]


def test_unreadable_ledger_alerts_instead_of_saying_all_applied(tmp_path):
    """Лежащий postgres не должен выглядеть как «расхождений нет»."""
    code, lines = _run(tmp_path, applied=[], existing=[],
                       files=PROD_0913_FILES, db_down=True)
    assert code == 1
    assert lines == ["учёт миграций не читается (postgres недоступен или нет schema_migrations)"]


def test_migration_without_probeable_objects_is_still_reported(tmp_path):
    """020 создаёт только VIEW — объекты не проверяются, но пропуск не прячется."""
    code, lines = _run(tmp_path, applied=[], existing=[],
                       files=["020_canonical_message_view"])
    assert code == 1
    assert lines == ["020_canonical_message_view: не записана в учёт "
                     "(объекты автоматически не проверяются)"]
