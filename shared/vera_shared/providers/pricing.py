"""DEPRECATED — kept as thin shim for backward compatibility.

All pricing now lives in `vera_shared.llm.registry`. New code should import
`cost_usd` from there directly.
"""
from vera_shared.llm.registry import cost_usd as _registry_cost


def cost_usd(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    """Old signature kept so existing multimodal.py callers don't break.
    Provider arg is ignored — registry resolves by model name alone."""
    return _registry_cost(model, tokens_in, tokens_out)
