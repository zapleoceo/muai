"""Graph repository — sync/upsert API for entities/aliases/memberships/etc.

**Границу этого слоя не пересекает ни один сервис.** Раньше докстринг
утверждал «это ЕДИНСТВЕННЫЙ слой, который трогает графовые таблицы», а
`gateway/query.py` джойнил `relationships` с `entities` прямо в
route-функции, и `ingestor_telegram/roster_sync.py` — `entity_aliases` с
`entities` в воркере. Оба переехали сюда (`list_relationships`,
`find_project_chats`, `get_entity`).

Что остаётся снаружи и почему: соседние модули пакета `graph/` —
`dedup.py`, `identity.py`, `dossiers.py`, `avatars.py`, `clusters.py` —
пишут по этим таблицам свой SQL. Это осознанно: `merge_entities`, разбор
коллизий и досье — операции, которые репозиторным CRUD'ом не выражаются,
и растаскивать их по обёрткам значило бы прятать транзакционную логику. Но
это ОДИН пакет: замена хранилища — правка `graph/`, а не поиск по всему
репозиторию. Формулировка «единственный слой» была про сервисы, и вот
теперь она верна.

Future swap to Neo4j is a new implementation behind the same interface (DIP).

Этот модуль — фасад: публичный API собран из модулей по агрегатам, и все
прежние импорты `vera_shared.graph.repo.<имя>` работают.

- `repo_entities` — запись сущностей и алиасов;
- `repo_entity_lookup` — поиск/резолв сущностей, `get_entity`, `list_relationships`;
- `repo_relationships` — запись и аудит связей;
- `repo_memberships` — членства и групповые чаты проектов;
- `repo_identity_nodes` — L3 identity nodes (стиль, цели, ценности);
- `repo_snapshot` — срез графа для /graph.
"""
from __future__ import annotations

from vera_shared.graph.repo_entities import upsert_entity, upsert_entity_linked
from vera_shared.graph.repo_entity_lookup import (
    find_entity_by_alias,
    find_entity_by_name,
    get_entity,
    list_relationships,
    resolve_entity_exact,
)
from vera_shared.graph.repo_identity_nodes import (
    get_global_style,
    get_style_for_listener,
    upsert_identity_node,
)
from vera_shared.graph.repo_memberships import (
    find_project_chats,
    list_members,
    upsert_membership,
)
from vera_shared.graph.repo_relationships import upsert_relationship
from vera_shared.graph.repo_snapshot import GRAPH_MAX_NODES, graph_snapshot

__all__ = [
    "GRAPH_MAX_NODES",
    "find_entity_by_alias",
    "find_entity_by_name",
    "find_project_chats",
    "get_entity",
    "get_global_style",
    "get_style_for_listener",
    "graph_snapshot",
    "list_members",
    "list_relationships",
    "resolve_entity_exact",
    "upsert_entity",
    "upsert_entity_linked",
    "upsert_identity_node",
    "upsert_membership",
    "upsert_relationship",
]
