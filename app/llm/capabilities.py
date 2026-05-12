CAP_CHAT = "chat"
CAP_EMBED = "embed"


PROVIDER_CAPABILITIES: dict[str, set[str]] = {
    "gemini": {CAP_CHAT, CAP_EMBED},
    "openai": {CAP_CHAT, CAP_EMBED},
    "groq": {CAP_CHAT},
    "deepseek": {CAP_CHAT},
}


def normalize_capabilities(provider: str, capabilities: list[str] | None) -> list[str]:
    caps = [c.strip().lower() for c in (capabilities or []) if c and c.strip()]
    if not caps:
        return sorted(PROVIDER_CAPABILITIES.get(provider, {CAP_CHAT}))
    return sorted(dict.fromkeys(caps))


def effective_capabilities(provider: str, raw: list[str] | None) -> set[str]:
    if raw:
        return set(normalize_capabilities(provider, raw))
    return set(PROVIDER_CAPABILITIES.get(provider, {CAP_CHAT}))
