"""Single source of truth for LLM configuration.

CHANGE ONE THING HERE → ENTIRE SYSTEM REBUILDS.

This module is the only place where:
  - models are listed (with their real API names)
  - prices are recorded
  - capability → provider routing is defined
  - paid keys are marked

Other modules (router.py, cost_guard.py, pool_clients.py, multimodal.py,
providers/pricing.py) MUST import from here. NEVER duplicate this data
elsewhere — that's how the 2026-06-01 $25 burn happened (two pricing
tables drifted apart by 4×).
"""
from __future__ import annotations

# ─── Model catalog ──────────────────────────────────────────────────────────
# Key = canonical model name (must match Google/DeepSeek/etc. API).
# Value = (input_price_per_1M_tokens, output_price_per_1M_tokens) in USD.
# A free model has (0.0, 0.0) — we don't gate or count what's free.
#
# Verified against provider docs 2026-06-04.
_MODELS: dict[str, tuple[float, float]] = {
    # Google Gemini paid tier
    "gemini-2.5-flash":         (0.075, 0.30),
    "gemini-2.5-pro":           (1.25,  5.00),
    "gemini-2.5-flash-lite":    (0.10,  0.40),
    # Anthropic
    "claude-haiku-4-5":         (1.00,  5.00),
    "claude-sonnet-4-5":        (3.00, 15.00),
    # DeepSeek (regular pricing; off-peak is half but we use upper bound)
    "deepseek-chat":            (0.27,  1.10),
    # Voyage embeddings (output token cost = 0, only input matters)
    "voyage-3":                 (0.06,  0.00),
    # OpenRouter free tier — explicit 0
    "openai/gpt-oss-120b:free": (0.0,   0.0),
    # Cerebras free tier — only gpt-oss-120b and zai-glm-4.7 are available
    # for free. gpt-oss-120b is wildly popular and its shared queue is
    # almost always overloaded (HTTP 429 queue_exceeded). zai-glm-4.7
    # (Zhipu GLM 4.7) is less hyped, queue is empty, comparable quality
    # for our structured-output use case.
    "zai-glm-4.7":              (0.0,   0.0),
    "gpt-oss-120b":             (0.0,   0.0),
    # Groq's namespaced gpt-oss-120b — SAME underlying open model as
    # Cerebras's, but Groq has it on its own infrastructure with healthy
    # rate limits AND it properly supports response_format=json_schema.
    # Groq's llama-3.3-70b-versatile DOES NOT support json_schema, so it
    # can't be used by Graphiti — kept here only for chat:fast fallback.
    "openai/gpt-oss-120b":      (0.0,   0.0),
    "llama-3.3-70b-versatile":  (0.0,   0.0),
}


# ─── Provider → model mapping ───────────────────────────────────────────────
# Which model our system uses for each provider. Change ONE line here to
# upgrade everything (router + Graphiti + multimodal).
PROVIDER_MODEL: dict[str, str] = {
    "gemini":     "gemini-2.5-flash",
    "deepseek":   "deepseek-chat",
    "anthropic":  "claude-haiku-4-5",
    "openrouter": "openai/gpt-oss-120b:free",
    "cerebras":   "gpt-oss-120b",
    "groq":       "openai/gpt-oss-120b",
    "voyage":     "voyage-3",
}


# ─── Capability routing ─────────────────────────────────────────────────────
# Fallback order when LiteLLM serves a capability. Earlier = preferred.
# Adding a provider = add it to relevant lists.
CAPABILITY_ORDER: dict[str, list[str]] = {
    # Free, fast Cerebras + Groq first — they have huge headroom (1M tok/day
    # × 4 keys for Cerebras alone) and let Gemini's small RPM window stay
    # available for Graphiti, which can ONLY use Gemini.
    "chat:fast":  ["cerebras", "groq", "gemini", "openrouter", "deepseek", "anthropic"],
    "prefilter":  ["cerebras", "groq", "gemini", "openrouter", "deepseek", "anthropic"],
    "chat:smart": ["cerebras", "groq", "openrouter", "deepseek", "anthropic", "gemini"],
    "chat:code":  ["cerebras", "groq", "openrouter", "deepseek", "anthropic", "gemini"],
}


# ─── Paid keys ──────────────────────────────────────────────────────────────
# (provider, label) tuples that point to paid/billed accounts.
# Used by:
#   - LiteLLM router (lower weight so free pool burns first)
#   - TokenPool selector (mark explicitly so it can prefer free)
#   - Cost guard (count toward daily $ cap)
#   - Dashboard (display paid badge)
PAID_KEYS: frozenset[tuple[str, str]] = frozenset({
    ("gemini", "demoniwwwe"),
    ("voyage", "demoniwwwe"),
})


# ─── Public API ─────────────────────────────────────────────────────────────


def model_for_provider(provider: str) -> str | None:
    """Which model do we use with this provider?"""
    return PROVIDER_MODEL.get(provider)


def is_paid(provider: str, label: str) -> bool:
    """Is this (provider, key-label) pair a paid account?"""
    return (provider, label) in PAID_KEYS


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate USD cost for a single call.

    Single source of truth — DO NOT duplicate this calculation elsewhere.

    Handles common name variants:
      - "gemini/gemini-2.5-flash" → "gemini-2.5-flash"
      - "GEMINI-2.5-FLASH"        → "gemini-2.5-flash"

    Unknown model → 0.0. We do not block calls we can't price; that's the
    caller's responsibility to add to _MODELS.
    """
    canonical = model.split("/")[-1].lower() if model else ""
    pin, pout = _MODELS.get(canonical, (0.0, 0.0))
    return (tokens_in / 1_000_000) * pin + (tokens_out / 1_000_000) * pout


def known_models() -> list[str]:
    """Useful for the dashboard / debug pages."""
    return sorted(_MODELS.keys())
