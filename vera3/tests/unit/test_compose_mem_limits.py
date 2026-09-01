"""Лимит памяти не может стоять ниже импорта сервиса.

bot-telegram уехал в ревью с `mem_limit: 160m` при 169 МБ RSS сразу после
`import bot_telegram.bot`: контейнер был бы убит OOM ещё до первого апдейта и
зациклился на `restart: unless-stopped`. Числа ниже — замер (python3.12, те же
версии зависимостей), а не оценка; тест держит инвариант «потолок с запасом
над импортом», чтобы следующая правка лимитов не вернула ту же аварию молча.

Замер здесь не повторяется: он требует всех зависимостей одиннадцати сервисов
и их обязательных env. Проверяется соотношение с зафиксированным полом.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[2] / "infra" / "docker-compose.yml"

#: сервис → RSS в МБ сразу после импорта точки входа (CMD из Dockerfile).
IMPORT_FLOOR_MB = {
    "bot-telegram": 169,
    "ingestor-telegram": 106,
    "ingestor-instagram": 82,
    "dashboard": 75,
    "gateway": 68,
    "brain-search": 66,
    "ingestor-slack": 59,
    "ingestor-gmail": 59,
    "ingestor-trello": 51,
    "media-worker": 50,
    "brain-triage": 50,
}

#: Минимальный множитель к импорту: сверху ложатся пул SQLAlchemy, буферы
#: httpx и пик обработки. Ниже 2× лимит перестаёт быть потолком и становится
#: вторым источником аварий.
MIN_HEADROOM = 2.0


def _limits() -> dict[str, int]:
    """{сервис: mem_limit в МБ} из compose. Без yaml — файл с якорями."""
    limits: dict[str, int] = {}
    service: str | None = None
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^  ([a-z0-9-]+):\s*$", line):
            service = m.group(1)
        elif (m := re.match(r"^\s+mem_limit:\s*(\d+)m", line)) and service:
            limits[service] = int(m.group(1))
    return limits


def test_compose_parsed():
    assert set(IMPORT_FLOOR_MB) <= set(_limits()), "сервис исчез из compose"


@pytest.mark.parametrize("service", sorted(IMPORT_FLOOR_MB))
def test_limit_clears_import_floor(service: str):
    limit = _limits()[service]
    floor = IMPORT_FLOOR_MB[service]
    assert limit >= floor * MIN_HEADROOM, (
        f"{service}: mem_limit {limit}m при импорте {floor} МБ — "
        f"нужно минимум {int(floor * MIN_HEADROOM)}m, иначе OOM на старте"
    )


def test_every_service_has_a_limit():
    """Сервис без лимита отдаёт выбор жертвы OOM-killer'у — в том числе на
    чужие стеки на том же боксе."""
    body = COMPOSE.read_text(encoding="utf-8")
    declared = set(re.findall(r"^  ([a-z0-9-]+):\s*$", body, re.M))
    missing = declared - set(_limits())
    assert not missing, f"без mem_limit: {sorted(missing)}"
