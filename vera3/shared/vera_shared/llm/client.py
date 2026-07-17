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
    chat_async_via_broker,
    chat_via_broker,
    embed_via_broker,
)
from vera_shared.llm.routing import Capability

log = logging.getLogger(__name__)


class LLMCallFailed(Exception):
    """Broker не ответил или вернул не-2xx после всех попыток."""


class LLMCoolingDown(LLMCallFailed):
    """Circuit breaker открыт: бюджет/пул этой capability исчерпан — вызов
    отклонён мгновенно, без создания джобы в брокере. Наследует LLMCallFailed,
    чтобы существующие except-ветки продолжали работать."""

    def __init__(self, capability: str, remaining_s: float):
        self.capability = capability
        self.remaining_s = remaining_s
        super().__init__(
            f"LLM circuit open for {capability}: cooling down "
            f"{remaining_s / 60:.0f} more min (budget cap / no provider)"
        )


def _require_broker() -> None:
    if not broker_enabled():
        raise LLMCallFailed(
            "BROKER_URL or BROKER_PROJECT_KEY not set — "
            "Vera runs in broker-only mode now (see vera3/docs/llm-broker.md)."
        )


async def _circuit_precheck(capability: str) -> None:
    from vera_shared.llm.circuit import llm_cooldown_remaining_s
    try:
        remaining = await llm_cooldown_remaining_s(capability)
    except Exception:  # noqa: BLE001 — breaker fail-open: сбой чтения кулдауна не блокирует вызов
        log.debug("circuit precheck failed", exc_info=True)
        return
    if remaining > 0:
        raise LLMCoolingDown(capability, remaining)


async def _circuit_note(capability: str, error: Exception) -> None:
    from vera_shared.llm.circuit import note_llm_failure
    try:
        await note_llm_failure(capability, str(error))
    except Exception:  # noqa: BLE001 — учёт кулдауна не должен маскировать ошибку вызова
        log.debug("circuit note failed", exc_info=True)


async def _circuit_reset(capability: str) -> None:
    from vera_shared.llm.circuit import reset_llm_cooldown
    try:
        await reset_llm_cooldown(capability)
    except Exception:  # noqa: BLE001
        log.debug("circuit reset failed", exc_info=True)


async def chat(
    messages: list[dict[str, Any]],
    *,
    capability: Capability = "chat:fast",
    response_format: dict | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    workflow: str | None = None,
    event_id: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Chat-completion через брокер. Бросает LLMCallFailed при провале брокера —
    вызывающий код (триаж, бот) сам решает, что делать (ретрай / pending).
    При открытом circuit breaker (кап бюджета) — мгновенный LLMCoolingDown."""
    _require_broker()
    await _circuit_precheck(capability)
    try:
        result = await chat_via_broker(
            messages=messages,
            capability=capability,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
            workflow=workflow,
            event_id=event_id,
        )
    except BrokerCallFailed as e:
        await _circuit_note(capability, e)
        raise LLMCallFailed(f"broker call failed: {e}") from e
    await _circuit_reset(capability)
    return result


async def chat_async(
    messages: list[dict[str, Any]],
    *,
    capability: Capability = "chat:fast",
    response_format: dict | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    workflow: str | None = None,
    event_id: int | None = None,
    poll_deadline_s: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Same contract as chat(), submit+poll (/v1/jobs) instead of holding a
    connection open — a slow provider delays the poll loop, not the caller.
    Additive: chat() keeps working unchanged. See docs/llm-broker.md.
    `poll_deadline_s` — per-call ожидание очереди (фоновые задачи, напр.
    ярлыки кластеров, могут ждать занятый free-пул дольше дефолтных 120с).
    При открытом circuit breaker — мгновенный LLMCoolingDown."""
    _require_broker()
    await _circuit_precheck(capability)
    try:
        result = await chat_async_via_broker(
            messages=messages,
            capability=capability,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
            workflow=workflow,
            event_id=event_id,
            poll_deadline_s=poll_deadline_s,
        )
    except BrokerCallFailed as e:
        await _circuit_note(capability, e)
        raise LLMCallFailed(f"broker async call failed: {e}") from e
    await _circuit_reset(capability)
    return result


async def embed(text: str | list[str]) -> list[list[float]]:
    """Voyage embedding через брокер. str → [str] (НЕ итерируем по char).
    Circuit breaker как у chat — embed-пул тоже бывает капнут."""
    _require_broker()
    if isinstance(text, list) and not text:
        return []
    await _circuit_precheck("embed")
    try:
        result = await embed_via_broker(text)
    except BrokerCallFailed as e:
        await _circuit_note("embed", e)
        raise LLMCallFailed(f"broker embed failed: {e}") from e
    await _circuit_reset("embed")
    return result


# Compat shim: старый код мог импортировать close_http_client из этого модуля,
# но HTTP-клиент теперь живёт в broker_client. Закрытие — там же.
async def close_http_client() -> None:
    from vera_shared.llm import broker_client as _bc

    if _bc._http is not None:
        await _bc._http.aclose()
        _bc._http = None
