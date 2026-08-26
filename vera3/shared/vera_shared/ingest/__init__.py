"""Ядро ингестора: то, что у каждого источника одинаково.

Источник приносит своё — транспорт к API, курсор, разбор полезной нагрузки в
спецификацию события. Всё остальное берёт отсюда, а не переписывает заново:

| Модуль | Что даёт |
|---|---|
| `writer` | `insert_events()` — запись с атомарным дедупом (`ON CONFLICT`) |
| `authors` | `sync_author_entities()` — автор события → person-сущность |
| `loop` | `poll_forever()` — цикл, который не падает без ключа |
| `authorship` | `resolve_author()` — таблица «источник → автор» для графа |

Порядок добавления источника — docs/sources.md, «Adding a new source».
"""
from vera_shared.ingest.authors import AuthorExtractor, sync_author_entities
from vera_shared.ingest.authorship import (
    AUTHOR_RESOLVERS,
    OWNER,
    Author,
    resolve_author,
)
from vera_shared.ingest.loop import AUTH_RETRY_S, poll_forever
from vera_shared.ingest.writer import insert_events, valid_spec

__all__ = [
    "AUTHOR_RESOLVERS",
    "AUTH_RETRY_S",
    "OWNER",
    "Author",
    "AuthorExtractor",
    "insert_events",
    "poll_forever",
    "resolve_author",
    "sync_author_entities",
    "valid_spec",
]
