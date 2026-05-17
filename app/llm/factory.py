from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.stub import StubProvider

_provider: LLMProvider | None = None
_router_provider: LLMProvider | None = None


def _build_provider(name: str) -> LLMProvider:
    settings = get_settings()

    if name == "auto":
        from app.llm.multi_provider import MultiProvider
        return MultiProvider()

    if name == "openai" and settings.openai_api_key:
        from app.llm.openai_provider import OpenAIProvider
        return OpenAIProvider(api_key=settings.openai_api_key)

    if name == "groq" and settings.groq_api_key:
        from app.llm.openai_provider import OpenAIProvider
        return OpenAIProvider(
            api_key=settings.groq_api_key,
            model="llama-3.3-70b-versatile",
            base_url="https://api.groq.com/openai/v1",
        )

    if name in ("gemini", "gemini-2.5-flash", "gemini-2.5-pro"):
        from app.llm.gemini_provider import GeminiProvider
        model = name if name.startswith("gemini-2") else "gemini-2.5-flash"
        return GeminiProvider(model=model)

    if name == "deepseek":
        from app.llm.deepseek_provider import DeepSeekProvider
        return DeepSeekProvider()

    return StubProvider()


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is not None:
        return _provider
    _provider = _build_provider(get_settings().llm_provider)
    return _provider


def get_fallback_llm_provider() -> LLMProvider | None:
    """Returns a fallback provider (DeepSeek) when the primary blocks content."""
    from app.llm.deepseek_provider import DeepSeekProvider
    return DeepSeekProvider()


def get_router_llm_provider() -> LLMProvider:
    global _router_provider
    settings = get_settings()
    router_name = settings.router_llm_provider
    if not router_name or router_name == settings.llm_provider:
        return get_llm_provider()
    if _router_provider is not None:
        return _router_provider
    _router_provider = _build_provider(router_name)
    return _router_provider
