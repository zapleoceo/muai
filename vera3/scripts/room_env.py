"""Конфиг room-слушателя: переменные окружения, иначе локальный файл вне репозитория.

Файл по умолчанию — `~/.vera/room.env` (путь меняет `ROOM_ENV_FILE`), строки `KEY=VALUE`,
`#` — комментарий. Переменная окружения всегда главнее файла. Значения не печатаются.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = Path.home() / ".vera" / "room.env"


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip().strip('"').strip("'")
    return found


def load_env(path: Path | None = None) -> None:
    """Дописать в os.environ ключи из файла, которых в окружении ещё нет."""
    source = path or Path(os.environ.get("ROOM_ENV_FILE", DEFAULT_ENV_FILE))
    for key, value in read_env_file(source).items():
        os.environ.setdefault(key, value)


def require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is not set: put it in the environment or in {DEFAULT_ENV_FILE}")
    return value
