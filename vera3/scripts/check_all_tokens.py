"""Реальная проверка каждого токена против провайдера API.

Различает:
- ✓ ALIVE     — провайдер отвечает 200, ключ валиден
- ◐ COOLDOWN  — rate-limit, ключ валиден, восстановится сам
- ✗ BANNED    — auth-error (401/403/abuse), нужно пересоздать
- ✗ NO_FUNDS  — paid и баланс пуст
- ?  UNKNOWN  — сетевая ошибка
"""
import asyncio
import os
from datetime import datetime

import httpx
from sqlalchemy import select

from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import TokenRow
from vera_shared.tokens.crypto import decrypt


PROVIDER_TESTS = {
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "method": "POST",
        "headers": lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        "body": {"model": "claude-haiku-4-5", "max_tokens": 1, "messages": [{"role": "user", "content": "."}]},
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "method": "POST",
        "headers": lambda k: {"Authorization": f"Bearer {k}", "content-type": "application/json"},
        "body": {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "."}], "max_tokens": 1},
    },
    "deepseek": {
        "url": "https://api.deepseek.com/chat/completions",
        "method": "POST",
        "headers": lambda k: {"Authorization": f"Bearer {k}", "content-type": "application/json"},
        "body": {"model": "deepseek-chat", "messages": [{"role": "user", "content": "."}], "max_tokens": 1},
    },
    "gemini": {
        "url": lambda k: f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={k}",
        "method": "POST",
        "headers": lambda k: {"content-type": "application/json"},
        "body": {"contents": [{"parts": [{"text": "."}]}], "generationConfig": {"maxOutputTokens": 1}},
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "method": "POST",
        "headers": lambda k: {"Authorization": f"Bearer {k}", "content-type": "application/json"},
        "body": {"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": "."}], "max_tokens": 1},
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "method": "POST",
        "headers": lambda k: {"Authorization": f"Bearer {k}", "content-type": "application/json"},
        "body": {"model": "openai/gpt-oss-120b:free", "messages": [{"role": "user", "content": "."}], "max_tokens": 1},
    },
    "voyage": {
        "url": "https://api.voyageai.com/v1/embeddings",
        "method": "POST",
        "headers": lambda k: {"Authorization": f"Bearer {k}", "content-type": "application/json"},
        "body": {"model": "voyage-3", "input": "."},
    },
    "manychat": {
        "url": "https://api.manychat.com/fb/page/getInfo",
        "method": "GET",
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "body": None,
    },
}


def classify(provider: str, status: int, body: str) -> tuple[str, str]:
    """Возвращает (status, hint)."""
    b = body.lower()
    if 200 <= status < 300:
        return "✓ ALIVE", ""
    if status == 429:
        return "◐ COOLDOWN", "rate limit"
    if status in (401, 403):
        if "insufficient balance" in b or "payment" in b or "depleted" in b:
            return "✗ NO_FUNDS", "balance/payment"
        if "abuse" in b or "restricted" in b or "suspended" in b:
            return "✗ BANNED", "abuse-flagged"
        if "invalid" in b or "expired" in b or "revok" in b:
            return "✗ BANNED", "key invalid/expired"
        return "✗ AUTH_FAIL", body[:80]
    if status == 402:
        return "✗ NO_FUNDS", "402 Payment Required"
    if "insufficient" in b or "balance" in b or "payment method" in b or "credits" in b:
        return "✗ NO_FUNDS", body[:80]
    if "abuse" in b or "restricted" in b or "suspended" in b:
        return "✗ BANNED", body[:80]
    if status >= 500:
        return "? SERVER_ERR", f"{status}"
    return "? OTHER", f"{status}: {body[:80]}"


async def check_one(c: httpx.AsyncClient, provider: str, label: str, key: str, tier: str):
    cfg = PROVIDER_TESTS.get(provider)
    if not cfg:
        print(f"{'?':2} {provider:11} {label:14} — no test config")
        return
    url = cfg["url"](key) if callable(cfg["url"]) else cfg["url"]
    headers = cfg["headers"](key)
    try:
        if cfg["method"] == "POST":
            r = await c.post(url, json=cfg["body"], headers=headers)
        else:
            r = await c.get(url, headers=headers)
        verdict, hint = classify(provider, r.status_code, r.text)
        print(f"{verdict:14} {provider:11} {label:14} [{tier:5}] HTTP {r.status_code:3}  {hint}")
    except Exception as e:
        print(f"? NETERR       {provider:11} {label:14} [{tier:5}] {type(e).__name__}: {str(e)[:60]}")


async def main():
    await init_engine()
    async with get_session() as s:
        tokens = (await s.execute(select(TokenRow).order_by(TokenRow.provider, TokenRow.id))).scalars().all()

    print(f"=== Probing {len(tokens)} tokens at {datetime.utcnow().isoformat()} ===\n")
    async with httpx.AsyncClient(timeout=20) as c:
        # Параллельно — по одному запросу на ключ
        await asyncio.gather(*(
            check_one(c, t.provider, t.label, decrypt(t.token_encrypted), t.tier)
            for t in tokens
        ))


if __name__ == "__main__":
    asyncio.run(main())
