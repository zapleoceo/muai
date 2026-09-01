"""Ни один СЕРВИС не ходит в графовые таблицы мимо `graph_repo`.

Докстринг repo.py обещал, что он единственный, кто их трогает. По факту
`gateway/query.py` джойнил relationships с entities прямо в route-функции,
а `ingestor_telegram/roster_sync.py` — entity_aliases с entities в воркере.
Смысл индирекции в том, чтобы смена хранилища была правкой одного пакета;
с двумя такими местами в разных сервисах это было неправдой.

Тест стережёт границу, а не стиль: внутри самого пакета `graph/` сырой SQL
разрешён — merge_entities и разбор коллизий репозиторным CRUD'ом не
выражаются.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

VERA3 = Path(__file__).resolve().parents[2]
GRAPH_TABLES = ("entities", "entity_aliases", "memberships", "relationships",
                "identity_nodes")

# Разрешено: сам пакет graph/ (включая repo.py) и тесты.
ALLOWED = (VERA3 / "shared" / "vera_shared" / "graph",)

_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(" + "|".join(GRAPH_TABLES) + r")\b",
    re.IGNORECASE,
)


def _service_sources() -> list[Path]:
    return [p for p in (VERA3 / "services").rglob("*.py") if "/tests/" not in str(p)]


def test_no_service_writes_raw_sql_against_graph_tables():
    offenders: list[str] = []
    for path in _service_sources():
        if any(str(path).startswith(str(a)) for a in ALLOWED):
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("--"):
                continue
            m = _TABLE_RE.search(line)
            if m:
                offenders.append(f"{path.relative_to(VERA3)}:{i}: {stripped[:90]}")

    assert not offenders, (
        "сервис ходит в графовые таблицы мимо graph_repo — перенеси запрос "
        "в vera_shared/graph/repo.py:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("name", [
    "get_entity", "list_relationships", "find_project_chats",
    "find_entity_by_name", "list_members", "graph_snapshot",
])
def test_repo_exposes_the_readers_services_need(name):
    """Если функцию удалят, сервисам придётся снова писать SQL самим —
    предыдущий тест поймает это уже как нарушение границы, а этот скажет
    прямо, чего не хватает."""
    from vera_shared.graph import repo
    assert callable(getattr(repo, name))
