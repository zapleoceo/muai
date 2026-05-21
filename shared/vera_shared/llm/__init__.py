"""LLM access via LiteLLM router.

Replaces the hand-rolled provider/registry/pool stack. Callers do:

    from vera_shared.llm import chat
    text, usage = await chat([{"role": "user", "content": "hi"}], system="…")

LiteLLM handles: provider fallback (gemini → deepseek → anthropic),
retry with backoff, key rotation across multiple instances of the same
provider, rate limit handling, cost tracking.
"""
from vera_shared.llm.router import chat, chat_with_meta

__all__ = ["chat", "chat_with_meta"]
