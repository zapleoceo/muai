from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.stub import StubProvider

_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is not None:
        return _provider

    settings = get_settings()

    if settings.llm_provider == "openai" and settings.openai_api_key:
        from app.llm.openai_provider import OpenAIProvider
        _provider = OpenAIProvider(api_key=settings.openai_api_key)

    elif settings.llm_provider == "groq" and settings.groq_api_key:
        from app.llm.openai_provider import OpenAIProvider
        _provider = OpenAIProvider(
            api_key=settings.groq_api_key,
            model="llama-3.3-70b-versatile",
            base_url="https://api.groq.com/openai/v1",
        )

    elif settings.llm_provider == "gemini" and settings.gemini_api_key:
        from app.llm.gemini_provider import GeminiProvider
        _provider = GeminiProvider(api_key=settings.gemini_api_key)

    else:
        _provider = StubProvider()

    return _provider
