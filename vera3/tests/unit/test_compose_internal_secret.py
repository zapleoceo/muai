"""Кто ходит во внутренние сервисы, обязан нести общий секрет.

brain-search и tools-сервер юзербота пускают только запросы с
`X-Internal-Secret`. У дашборда в compose была ссылка на brain-search, но не
было секрета: «Спросить Веру» отвечала 502 «invalid internal secret»
(03.10.2026), и заметили это только глазами в браузере.
"""
from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "infra" / "docker-compose.yml"

#: Сервисы, которые проверяют секрет у входящих запросов.
GUARDED_HOSTS = ("brain-search", "ingestor-telegram", "gateway")


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _calls_guarded(env: dict) -> list[str]:
    return [k for k, v in env.items()
            if isinstance(v, str) and any(f"http://{h}:" in v for h in GUARDED_HOSTS)]


def test_every_caller_of_an_internal_service_has_the_secret():
    missing = {name: _calls_guarded(svc.get("environment") or {})
               for name, svc in _services().items()
               if _calls_guarded(svc.get("environment") or {})
               and "INTERNAL_SECRET" not in (svc.get("environment") or {})}
    assert missing == {}


def test_dashboard_reaches_search_with_the_secret():
    env = _services()["dashboard"]["environment"]
    assert env["SEARCH_URL"].startswith("http://brain-search:")
    assert "INTERNAL_SECRET" in env
