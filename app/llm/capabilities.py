CAP_CHAT = "chat"
CAP_EMBED = "embed"


PROVIDER_CAPABILITIES: dict[str, set[str]] = {
    "gemini": {CAP_CHAT, CAP_EMBED},
    "openai": {CAP_CHAT, CAP_EMBED},
    "groq": {CAP_CHAT},
    "deepseek": {CAP_CHAT},
}


def normalize_capabilities(provider: str, capabilities: list[str] | None) -> list[str]:
    allowed = PROVIDER_CAPABILITIES.get(provider, {CAP_CHAT})
    caps = [c.strip().lower() for c in (capabilities or []) if c and c.strip()]
    if not caps:
        return sorted(allowed)
    unique = list(dict.fromkeys(caps))
    filtered = [c for c in unique if c in allowed]
    return sorted(filtered or allowed)


def effective_capabilities(provider: str, raw: list[str] | None) -> set[str]:
    if raw:
        return set(normalize_capabilities(provider, raw))
    return set(PROVIDER_CAPABILITIES.get(provider, {CAP_CHAT}))
