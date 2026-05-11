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
                    raise RuntimeError(f"Gemini HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                # Network error — penalise token
                await manager.on_error(token)
                raise RuntimeError(f"Gemini network error: {exc.reason}") from exc
            except _GeminiContentError:
                # Response arrived but content was blocked or empty — token is fine
                raise
            except Exception as exc:
                # Unexpected error — penalise token
                await manager.on_error(token)
                raise RuntimeError(f"Gemini unexpected error: {exc}") from exc

        raise RuntimeError("Gemini: exhausted retries")

    @staticmethod
    def _call(req: urllib.request.Request) -> str:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())

        # Prompt-level block (e.g. SAFETY before any candidate generated)
        feedback = data.get("promptFeedback", {})
        if feedback.get("blockReason"):
            raise _GeminiContentError(f"prompt blocked: {feedback['blockReason']}")

        candidates = data.get("candidates", [])
        if not candidates:
            raise _GeminiContentError("no candidates in response")

        candidate = candidates[0]
        finish = candidate.get("finishReason", "STOP")
        content = candidate.get("content", {})
        parts = content.get("parts", [])

        if not parts:
            # Safety block or other non-content finish
            raise _GeminiContentError(f"empty response (finishReason={finish})")

        return parts[0].get("text", "")


class _GeminiContentError(RuntimeError):
    """Response received but content is absent or blocked. Token is not at fault."""
