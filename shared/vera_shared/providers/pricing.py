"""USD per 1M tokens, per provider/model. Conservative estimates."""

PRICING: dict[str, tuple[float, float]] = {
    # provider:model → (input, output) USD per 1M tokens
    "gemini:gemini-flash-lite-latest": (0.10, 0.40),
    "gemini:gemini-2.5-flash-lite":    (0.10, 0.40),
    "gemini:gemini-2.5-flash":         (0.30, 2.50),
    "deepseek:deepseek-chat":          (0.27, 1.10),
    "anthropic:claude-haiku-4-5":      (1.00, 5.00),
    "voyage:voyage-3":                 (0.06, 0.00),
}


def cost_usd(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    key = f"{provider}:{model}"
    rate_in, rate_out = PRICING.get(key, (0.0, 0.0))
    return (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000
