"""Capability type — единственное что осталось от старого роутинга.

Broker сам решает chain'ы и cost guard'ы; Vera-сторона просто передаёт
capability вместе с запросом.
"""
from __future__ import annotations

from typing import Literal

Capability = Literal[
    "chat:fast",     # быстрые ответы, триаж, простой dialog
    "chat:smart",    # сложные задачи, синтез, paper-quality
    "chat:code",     # программирование, code review
    "prefilter",     # лёгкий фильтр перед более тяжёлой обработкой
    "structured",    # строгий json_schema (Graphiti, extraction)
    "vision",        # мультимодальное
    "embedding",     # эмбеддинги
]
