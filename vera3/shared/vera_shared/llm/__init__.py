"""LLM layer: registry (SSOT), routing policy, cost guard, client wrapper."""
from vera_shared.llm.registry import (
    PROVIDER_MODEL,
    PROVIDER_TIER,
    PROVIDER_BASE_URL,
    PROVIDER_SUPPORTS_JSON_SCHEMA,
    cost_usd,
    is_paid_provider,
    model_for_provider,
    known_providers,
    known_models,
)
from vera_shared.llm.routing import RoutingPolicy, Capability
from vera_shared.llm.cost_guard import (
    DailyBudgetExceeded,
    can_call_paid,
    estimate_cost,
)

__all__ = [
    "PROVIDER_MODEL",
    "PROVIDER_TIER",
    "PROVIDER_BASE_URL",
    "PROVIDER_SUPPORTS_JSON_SCHEMA",
    "cost_usd",
    "is_paid_provider",
    "model_for_provider",
    "known_providers",
    "known_models",
    "RoutingPolicy",
    "Capability",
    "DailyBudgetExceeded",
    "can_call_paid",
    "estimate_cost",
]
