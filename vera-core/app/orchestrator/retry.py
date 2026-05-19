import logging

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a prompt engineer. Rewrite the user prompt to be more specific "
    "and actionable, fixing issues from previous failed attempts. "
    "Return only the rewritten prompt, no explanation."
)


async def optimize_prompt(original: str, results: dict[str, str], attempt: int) -> str:
    errors = {k: v for k, v in results.items() if v.startswith("ERROR:")}
    failures = "\n".join(f"- {k}: {v}" for k, v in errors.items()) if errors else "Response quality was too low"

    prompt = (
        f"Original prompt (attempt {attempt}):\n{original}\n\n"
        f"Issues:\n{failures}\n\n"
        "Rewrite the prompt to address these issues."
    )

    try:
        from vera_shared.providers.registry import get_registry
        registry = get_registry()
        rewritten, _, _ = await registry.chat("prefilter", [{"role": "user", "content": prompt}], system=_SYSTEM)
        return rewritten.strip() or original
    except Exception as exc:
        log.warning("optimize_prompt failed: %s", exc)
        return original
