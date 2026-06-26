"""LLM layer — broker-only.

Все вызовы (chat + embed) идут через AIbroker. Vera не несёт у себя ни
routing-chain'ов, ни cost-guard'ов, ни provider-registry — это всё на
стороне брокера.
"""
from vera_shared.llm.broker_client import (
    BrokerCallFailed,
    broker_enabled,
    chat_via_broker,
    embed_via_broker,
)
from vera_shared.llm.client import LLMCallFailed, chat, close_http_client, embed
from vera_shared.llm.routing import Capability

__all__ = [
    "Capability",
    "LLMCallFailed",
    "BrokerCallFailed",
    "chat",
    "embed",
    "broker_enabled",
    "chat_via_broker",
    "embed_via_broker",
    "close_http_client",
]
