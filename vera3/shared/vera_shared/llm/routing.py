"""Routing policy — какие провайдеры в каком порядке для каждой capability.

ПРИНЦИП: free всегда впереди paid. Внутри free — самые большие квоты первыми.

Использование: воркер запрашивает `RoutingPolicy.chain_for(capability, require_json_schema=True)`
и получает упорядоченный список провайдеров. Пробует по очереди, использует
первого доступного (есть ключ, не в cooldown, не превысил cap).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vera_shared.llm.registry import (
    PROVIDER_TIER,
    is_paid_provider,
    supports_json_schema,
)

# Capability — логическая роль вызова.
Capability = Literal[
    "chat:fast",     # быстрые ответы, триаж, простой dialog
    "chat:smart",    # сложные задачи, синтез, paper-quality
    "chat:code",     # программирование, code review
    "prefilter",     # лёгкий фильтр перед более тяжёлой обработкой
    "structured",    # требует строгий json_schema (Graphiti, extraction)
    "vision",        # мультимодальное (картинки, аудио)
    "embedding",     # эмбеддинги
]


@dataclass(frozen=True)
class ProviderChoice:
    """Один шаг в fallback chain."""
    provider: str
    tier: str  # 'free' | 'paid' | 'trial'

    @property
    def is_paid(self) -> bool:
        return self.tier == "paid"

    @property
    def is_free(self) -> bool:
        return self.tier == "free"


# ─── Базовые цепочки ────────────────────────────────────────────────────────
# Free провайдеры первыми. Trial вторыми. Paid — последний резерв.

_BASE_CHAINS: dict[Capability, list[str]] = {
    # Быстрые ответы: free большие пулы первые. DeepSeek (paid, $0.27/1M)
    # повышен в ранке — latency 1.5s против 22s у openrouter:free.
    # При активном backfill это даёт ×10 throughput за copейки.
    "chat:fast": [
        "cerebras", "groq", "gemini",
        "deepseek",  # paid но дешёвый и быстрый — после free, перед slow free
        "openrouter", "sambanova", "nvidia", "mistral",
        "anthropic",  # trial
        "openai",  # paid резерв
    ],
    # Умные запросы — те же провайдеры но смещён акцент на качество.
    "chat:smart": [
        "cerebras", "groq", "gemini", "sambanova",
        "anthropic",  # trial — хорошие для синтеза
        "openrouter", "nvidia", "mistral",
        "openai", "deepseek",  # paid
    ],
    # Код: DeepSeek хорош в коде, но платный. Anthropic Claude — лучший но дорогой.
    # Free сначала (cerebras/groq/openrouter/gemini/nvidia/sambanova), потом trial, потом paid.
    "chat:code": [
        "cerebras", "groq", "openrouter", "gemini", "nvidia", "sambanova",
        "anthropic",  # trial
        "deepseek", "openai",  # paid
    ],
    # Предфильтр — нам важнее latency чем quality, плюс хочется free
    "prefilter": [
        "cerebras", "groq", "gemini", "sambanova", "nvidia",
        "openrouter", "mistral",
    ],
    # Structured output (Graphiti требует json_schema) — фильтруется ниже
    "structured": [
        "cerebras", "groq", "gemini",
        "openrouter", "sambanova", "nvidia", "mistral",
        "anthropic", "openai",  # paid но точно поддерживают
    ],
    # Vision — Gemini Flash multimodal, OpenAI gpt-5
    "vision": [
        "gemini",
        "anthropic", "openai",
    ],
    # Embedding — только voyage у нас. Когда добавим cohere/openai — здесь.
    "embedding": [
        "voyage",
    ],
}


class RoutingPolicy:
    """SSOT для маршрутизации запросов между провайдерами."""

    @staticmethod
    def chain_for(
        capability: Capability,
        *,
        require_json_schema: bool = False,
        include_paid: bool = True,
    ) -> list[ProviderChoice]:
        """Вернуть упорядоченную цепочку провайдеров.

        Args:
            capability: тип задачи
            require_json_schema: если True — фильтруем провайдеров которые
                не поддерживают строгий json_schema
            include_paid: если False — только free/trial (для backfill режима)
        """
        if capability not in _BASE_CHAINS:
            raise ValueError(f"Unknown capability: {capability}")

        providers = _BASE_CHAINS[capability]

        out: list[ProviderChoice] = []
        for p in providers:
            tier = PROVIDER_TIER.get(p, "paid")
            if not include_paid and tier == "paid":
                continue
            if require_json_schema and not supports_json_schema(p):
                continue
            out.append(ProviderChoice(provider=p, tier=tier))
        return out

    @staticmethod
    def verify_free_first(capability: Capability) -> None:
        """Инвариант: все free провайдеры идут раньше всех paid."""
        chain = RoutingPolicy.chain_for(capability, include_paid=True)
        last_free_idx = -1
        first_paid_idx = len(chain)
        for i, choice in enumerate(chain):
            if choice.is_free:
                last_free_idx = i
            if choice.is_paid and first_paid_idx == len(chain):
                first_paid_idx = i
        if last_free_idx >= first_paid_idx:
            raise AssertionError(
                f"Capability {capability}: paid провайдер на позиции "
                f"{first_paid_idx} идёт раньше free на позиции {last_free_idx}"
            )
