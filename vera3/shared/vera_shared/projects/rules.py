"""Правила принадлежности к проектам — источник истины: папки Telegram + имена.

Чистая логика (без БД), чтобы покрыть тестами. Оркестрация (чтение папок,
запись в project_membership, простановка events.project) — в sync_projects.py.

Дизайн, который просил Дима:
- Папка Telegram «ItStep» → все её чаты = проект itstep.
- Любой чат с «Veranda»/«Веранда» в названии = проект veranda.
- Люди наследуют проект(ы) из чатов, где они участвуют (могут быть в нескольких).
- Почтовый аккаунт *@itstep.org = проект itstep.
"""
from __future__ import annotations

import os

# Владелец (Дима) — ось всего, из «людей проекта» исключается.
OWNER_TG_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "169510539"))

# Папка Telegram (нижний регистр без пробелов) → проект.
FOLDER_TO_PROJECT: dict[str, str] = {
    "itstep": "itstep",
}

# Проект → подстроки в названии чата (нижний регистр). Union с папками.
NAME_RULES: dict[str, list[str]] = {
    "veranda": ["veranda", "веранда"],
}

# Проект → ILIKE-паттерны по gmail-аккаунту.
ACCOUNT_RULES: dict[str, list[str]] = {
    "itstep": ["%itstep.org%"],
}

VALID_PROJECTS = {"itstep", "veranda", "family", "personal", "news", "other"}


def folder_to_project(folder_title: str | None) -> str | None:
    """Название папки Telegram → проект (или None)."""
    if not folder_title:
        return None
    key = folder_title.strip().lower().replace(" ", "").replace("-", "")
    return FOLDER_TO_PROJECT.get(key)


def match_name(chat_title: str | None) -> str | None:
    """Проект по подстроке в названии чата (первое совпадение)."""
    if not chat_title:
        return None
    low = chat_title.lower()
    for project, subs in NAME_RULES.items():
        if any(s in low for s in subs):
            return project
    return None


# Порог -100-префикса супергрупп/каналов: peer -100XXXXXXXXXX хранится то как
# bare XXXXXXXXXX, то как положительное 100XXXXXXXXXX. Сводим к bare.
_SUPERGROUP_PREFIX = 1_000_000_000_000

# SQL-выражение канонизации chat_id (подставляй alias таблицы, напр. 'e').
def chat_id_canon_sql(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    a = f"ABS(({p}metadata->>'chat_id')::bigint)"
    return f"(CASE WHEN {a} > {_SUPERGROUP_PREFIX} THEN {a} - {_SUPERGROUP_PREFIX} ELSE {a} END)"


def chat_key(chat_id: int | str) -> int:
    """Канонический ключ чата. abs + снятие -100-префикса супергрупп/каналов,
    чтобы формы 3889942420 и 1003889942420 сводились к одному ключу."""
    a = abs(int(chat_id))
    if a > _SUPERGROUP_PREFIX:
        a -= _SUPERGROUP_PREFIX
    return a


def is_owner(sender_id: int | str | None) -> bool:
    if sender_id is None:
        return False
    try:
        return int(sender_id) == OWNER_TG_ID
    except (ValueError, TypeError):
        return False
