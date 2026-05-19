from vera_shared.providers.base import BaseProvider, ProviderError
from vera_shared.providers.deepseek import get_deepseek
from vera_shared.providers.gemini import get_gemini
from vera_shared.providers.voyage import get_voyage
from vera_shared.tokens.pool import TokensExhausted

CAPABILITY_PROVIDERS: dict[str, list[str]] = {
    "chat:fast": ["gemini"],
    "prefilter": ["gemini"],
    "chat:smart": ["deepseek", "gemini"],
    "chat:code": ["deepseek", "gemini"],
    "embed": ["voyage"],
}

_PROVIDER_MAP: dict[str, BaseProvider] = {}


def _get_provider(name: str) -> BaseProvider:
    if name not in _PROVIDER_MAP:
        if name == "gemini":
            _PROVIDER_MAP[name] = get_gemini()
        elif name == "deepseek":
            _PROVIDER_MAP[name] = get_deepseek()
        elif name == "voyage":
            _PROVIDER_MAP[name] = get_voyage()
        else:
            raise ValueError(f"Unknown provider: {name}")
    return _PROVIDER_MAP[name]


class ProviderRegistry:
    async def chat(
        self, capability: str, messages: list[dict], system: str | None = None
    ) -> tuple[str, int, int]:
        provider_names = CAPABILITY_PROVIDERS.get(capability, ["gemini"])
        last_exc: Exception | None = None

        for name in provider_names:
            provider = _get_provider(name)
            try:
                return await provider.chat(messages, capability, system=system)
            except (TokensExhausted, ProviderError) as exc:
                last_exc = exc

        raise last_exc or ProviderError(f"No provider available for {capability}")

    async def embed(self, text: str) -> list[float]:
        provider_names = CAPABILITY_PROVIDERS.get("embed", ["voyage"])
        last_exc: Exception | None = None

        for name in provider_names:
            provider = _get_provider(name)
            try:
                return await provider.embed(text)
            except (TokensExhausted, ProviderError) as exc:
                last_exc = exc

        raise last_exc or ProviderError("No embed provider available")


_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry
