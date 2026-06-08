"""Single source of truth для LLM конфигурации.

ИЗМЕНИ ЗДЕСЬ → ВСЯ СИСТЕМА ПЕРЕСТРОИТСЯ.

Это единственное место где описаны:
  - какие провайдеры существуют (с base URL)
  - какие модели они отдают
  - какой tier (free / paid / trial)
  - поддерживают ли response_format=json_schema (для Graphiti/structured)
  - цены за токены

Другие модули (routing, cost_guard, client wrapper, pool_clients) ИМПОРТИРУЮТ
из этого модуля. НЕ дублировать эти данные в других местах.

Урок Vera 2.0 ($25 burn 2026-06-01): две таблицы цен разъехались в 4×.
В Vera 3.0 — единая структура с автоматической верификацией в тестах.
"""
from __future__ import annotations

from typing import Literal

# Tier — free / paid / trial
Tier = Literal["free", "paid", "trial"]

# ─── Каталог моделей ────────────────────────────────────────────────────────
# Ключ = каноническое имя модели (должно совпадать с API провайдера).
# Значение = (input_price_per_1M, output_price_per_1M) в USD.
# Free model = (0.0, 0.0).
_MODELS: dict[str, tuple[float, float]] = {
    # Google Gemini
    "gemini-2.5-flash":         (0.075,  0.30),
    "gemini-2.5-pro":           (1.25,   5.00),
    "gemini-2.5-flash-lite":    (0.10,   0.40),
    # Anthropic
    "claude-haiku-4-5":         (1.00,   5.00),
    "claude-sonnet-4-5":        (3.00,  15.00),
    # DeepSeek
    "deepseek-chat":            (0.27,   1.10),
    # Voyage (embedding, output = 0)
    "voyage-3":                 (0.06,   0.00),
    "voyage-3-large":           (0.18,   0.00),
    # Cerebras / Groq / OpenRouter (free + paid OpenAI-compatible)
    "gpt-oss-120b":             (0.0,    0.0),       # cerebras free
    "openai/gpt-oss-120b":      (0.0,    0.0),       # groq free namespace
    "openai/gpt-oss-120b:free": (0.0,    0.0),       # openrouter free
    "zai-glm-4.7":              (0.0,    0.0),       # cerebras free
    "llama-3.3-70b-versatile":  (0.0,    0.0),       # groq free
    # OpenAI
    "gpt-5-mini":               (0.15,   0.60),
    "gpt-5-nano":               (0.05,   0.20),
    # SambaNova (free)
    "Meta-Llama-3.3-70B-Instruct": (0.0,  0.0),
    # NVIDIA NIM (free tier per key)
    "meta/llama-3.3-70b-instruct": (0.0,  0.0),
    # Mistral
    "mistral-small-latest":     (0.20,   0.60),
    "open-mistral-7b":          (0.0,    0.0),       # experimental free
}


# ─── Провайдеры — конфиг ───────────────────────────────────────────────────
# Описание одного провайдера: какую модель использовать, какой tier (по
# дефолту — переопределяется per-key через tokens.tier в БД), base URL,
# поддерживает ли строгий json_schema (важно для Graphiti).

PROVIDER_MODEL: dict[str, str] = {
    "gemini":     "gemini-2.5-flash",
    "anthropic":  "claude-haiku-4-5",
    "deepseek":   "deepseek-chat",
    "openrouter": "openai/gpt-oss-120b:free",
    "cerebras":   "gpt-oss-120b",
    "groq":       "openai/gpt-oss-120b",
    "voyage":     "voyage-3",
    "openai":     "gpt-5-mini",
    "sambanova":  "Meta-Llama-3.3-70B-Instruct",
    "nvidia":     "meta/llama-3.3-70b-instruct",
    "mistral":    "mistral-small-latest",
}

PROVIDER_TIER: dict[str, Tier] = {
    "gemini":     "free",   # default free key есть; per-key override через tokens.tier
    "anthropic":  "trial",  # есть trial credits, потом paid
    "deepseek":   "paid",
    "openrouter": "free",
    "cerebras":   "free",
    "groq":       "free",
    "voyage":     "free",   # 200M токенов/мес free per account
    "openai":     "paid",
    "sambanova":  "free",
    "nvidia":     "free",   # 1000 calls/key life-time
    "mistral":    "free",   # experimental tier
}

PROVIDER_BASE_URL: dict[str, str] = {
    "gemini":     "https://generativelanguage.googleapis.com/v1beta",
    "anthropic":  "https://api.anthropic.com",
    "deepseek":   "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "cerebras":   "https://api.cerebras.ai/v1",
    "groq":       "https://api.groq.com/openai/v1",
    "voyage":     "https://api.voyageai.com/v1",
    "openai":     "https://api.openai.com/v1",
    "sambanova":  "https://api.sambanova.ai/v1",
    "nvidia":     "https://integrate.api.nvidia.com/v1",
    "mistral":    "https://api.mistral.ai/v1",
}

# Поддерживает ли провайдер response_format=json_schema (строгий).
# Это важно для Graphiti и любых задач со структурированным выводом.
# Verified by direct probes 2026-06-08.
PROVIDER_SUPPORTS_JSON_SCHEMA: dict[str, bool] = {
    "gemini":     True,
    "anthropic":  True,
    "deepseek":   False,    # только json_object, не json_schema
    "openrouter": True,     # зависит от модели, но gpt-oss-120b:free поддерживает
    "cerebras":   True,     # gpt-oss-120b поддерживает (zai-glm не парсится корректно)
    "groq":       True,     # openai/gpt-oss-120b поддерживает (llama-3.3 — нет)
    "voyage":     False,    # embedder, не chat
    "openai":     True,
    "sambanova":  True,
    "nvidia":     True,
    "mistral":    True,     # на small-latest и выше
}


# ─── Публичный API ──────────────────────────────────────────────────────────


def known_providers() -> list[str]:
    return sorted(PROVIDER_MODEL.keys())


def known_models() -> list[str]:
    return sorted(_MODELS.keys())


def model_for_provider(provider: str) -> str | None:
    return PROVIDER_MODEL.get(provider)


def is_paid_provider(provider: str) -> bool:
    """Дефолтная классификация по провайдеру. Per-key — через tokens.tier."""
    return PROVIDER_TIER.get(provider) == "paid"


def supports_json_schema(provider: str) -> bool:
    """Можно ли через этого провайдера слать response_format=json_schema."""
    return PROVIDER_SUPPORTS_JSON_SCHEMA.get(provider, False)


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Стоимость одного вызова в USD по нашим прайс-таблицам.

    Обрабатывает варианты имени:
      'gemini/gemini-2.5-flash' → 'gemini-2.5-flash'
      'GEMINI-2.5-FLASH'         → нормализуется
      'openai/gpt-oss-120b'      → matches openai/gpt-oss-120b (с префиксом)

    Unknown model → 0.0 (вызывающий обязан добавить в _MODELS).
    """
    if not model:
        return 0.0
    # Сначала пробуем как есть (для namespaced — openai/gpt-oss-120b и т.п.)
    if model in _MODELS:
        pin, pout = _MODELS[model]
    else:
        # Иначе берём последний сегмент и нормализуем lowercase
        canonical = model.split("/")[-1].lower()
        pin, pout = _MODELS.get(canonical, (0.0, 0.0))
    return (tokens_in / 1_000_000) * pin + (tokens_out / 1_000_000) * pout
