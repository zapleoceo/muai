"""LLM client — broker-only.

Все LLM-вызовы Vera идут через AIbroker (https://aib.zapleo.com). Broker
сам делает routing, выбор ключа, cost guard, cooldown'ы и retry. Vera
просто отдаёт `messages + capability`.

У Веры нет собственных LLM-ключей — таблица `tokens` удалена (миграция
008). Управление ключами целиком в дашборде брокера. Алерт о падении
брокера присылает `vera3-monitor.sh` через Telegram через ~10 минут.
"""
from __future__ import annotations

import logging
from typing import Any

from vera_shared.llm.broker_client import (
    BrokerCallFailed,
    broker_enabled,
    chat_via_broker,
    embed_via_broker,
)
from vera_shared.llm.routing import Capability

log = logging.getLogger(__name__)


class LLMCallFailed(Exception):
    """Broker не ответил или вернул не-2xx после всех попыток."""


def _require_broker() -> None:
    if not broker_enabled():
        raise LLMCallFailed(
            "BROKER_URL or BROKER_PROJECT_KEY not set — "
            "Vera runs in broker-only mode now (see vera3/docs/llm-broker.md)."
        )


async def chat(
    messages: list[dict[str, Any]],
    *,
    capability: Capability = "chat:fast",
    require_json_schema: bool = False,
    response_format: dict | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    workflow: str | None = None,
    event_id: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Chat-completion через брокер. Бросает LLMCallFailed при провале брокера —
    вызывающий код (триаж, бот) сам решает, что делать (ретрай / pending)."""
    _require_broker()
    try:
        return await chat_via_broker(
            messages=messages,
            capability=capability,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
            workflow=workflow,
            event_id=event_id,
        )
    except BrokerCallFailed as e:
        raise LLMCallFailed(f"broker call failed: {e}") from e


async def embed(text: str | list[str]) -> list[list[float]]:
    """Voyage embedding через брокер. str → [str] (НЕ итерируем по char)."""
    _require_broker()
    if isinstance(text, list) and not text:
        return []
    try:
        return await embed_via_broker(text)
    except BrokerCallFailed as e:
        raise LLMCallFailed(f"broker embed failed: {e}") from e


# Compat shim: старый код мог импортировать close_http_client из этого модуля,
# но HTTP-клиент теперь живёт в broker_client. Закрытие — там же.
async def close_http_client() -> None:
    from vera_shared.llm import broker_client as _bc

    if _bc._http is not None:
        await _bc._http.aclose()
        _bc._http = None
