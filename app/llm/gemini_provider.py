import asyncio
import json
import logging
import urllib.error
import urllib.request

from app.llm.base import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_MAX_RETRIES = 3


class GeminiProvider(LLMProvider):
    def __init__(self, model: str = "gemini-2.5-flash"):
        self._model = model

    async def complete(self, messages: list[LLMMessage], system_prompt: str = "") -> str:
        from app.services.tokens import get_token_manager
        manager = get_token_manager()

        contents = [
            {"role": "model" if m.role == "assistant" else "user",
             "parts": [{"text": m.content}]}
            for m in messages
        ]
        payload: dict = {"contents": contents}
        if system_prompt:
            payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
        body = json.dumps(payload).encode()

        for attempt in range(_MAX_RETRIES):
            token = await manager.next_token()
            if not token:
                raise RuntimeError("No Gemini tokens configured. Add one at /api/admin/tokens.")

            url = f"{_BASE_URL}/{self._model}:generateContent?key={token}"
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                text = await asyncio.get_event_loop().run_in_executor(None, self._call, req)
                return text
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    await manager.on_rate_limit(token)
                    logger.warning("Gemini 429 on attempt %d, rotating token", attempt + 1)
                    if attempt == _MAX_RETRIES - 1:
                        raise RuntimeError("All Gemini tokens rate-limited") from exc
                else:
                    await manager.on_error(token)
                    raise
            except Exception:
                await manager.on_error(token)
                raise

        raise RuntimeError("Gemini: exhausted retries")

    @staticmethod
    def _call(req: urllib.request.Request) -> str:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data["candidates"][0]["content"]["parts"][0]["text"]
