from app.orchestrator.prefilter import Intent

_RETRY_HINTS = [
    "",
    "Previous attempt was incomplete. Be more specific and detailed.",
    "Previous two attempts failed quality check. Provide a thorough, structured response.",
]


def build_prompts(intent: Intent, original_text: str, attempt: int) -> dict[str, str]:
    retry_hint = _RETRY_HINTS[min(attempt - 1, len(_RETRY_HINTS) - 1)]

    base = (
        f"Task: {intent.summary}\n"
        f"Original request: {original_text}\n"
    )

    if intent.context:
        ctx_lines = "\n".join(f"  {k}: {v}" for k, v in intent.context.items())
        base += f"Context:\n{ctx_lines}\n"

    if retry_hint:
        base += f"\nNote: {retry_hint}"

    return {agent_id: base for agent_id in intent.target_agents}
