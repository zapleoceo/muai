import asyncio
import json
import logging
import urllib.error
import urllib.request

from app.llm.base import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_MAX_RETRIES = 3


class GeminiContentError(RuntimeError):
    """Response received but content is absent or blocked. Token is not at fault."""
    def __init__(self, reason: str, finish_reason: str = "", safety_ratings: list | None = None):
        self.reason = reason
        self.finish_reason = finish_reason
        self.safety_ratings = safety_ratings or []
        super().__init__(reason)


class GeminiProvider(LLMProvider):
    def __init__(self, model: str = "gemini-2.5-flash"):
        self._model = model

    async def complete(self, messages: list[LLMMessage], system_prompt: str = "") -> str:
        from app.services.tokens import get_token_manager
        manager = get_token_manager()

        body = self._build_body(messages, system_prompt)

        for attempt in range(_MAX_RETRIES):
            lease = await manager.next_token("chat", provider="gemini")
            if not lease:
                raise RuntimeError("No Gemini tokens configured. Add one at /api/admin/tokens.")

            url = f"{_BASE_URL}/{self._model}:generateContent?key={lease.token}"
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                return await asyncio.get_event_loop().run_in_executor(None, self._call, req)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    await manager.on_rate_limit(lease.id)
                    logger.warning("Gemini 429 on attempt %d, rotating token", attempt + 1)
                    if attempt == _MAX_RETRIES - 1:
                        raise RuntimeError("All Gemini tokens rate-limited") from exc
                else:
                    body_text = exc.read().decode(errors="replace") if hasattr(exc, "read") else ""
                    await manager.on_error(lease.id)
                    raise RuntimeError(f"Gemini HTTP {exc.code}: {body_text[:200]}") from exc
            except urllib.error.URLError as exc:
                await manager.on_error(lease.id)
                raise RuntimeError(f"Gemini network error: {exc.reason}") from exc
            except GeminiContentError:
                raise
            except Exception as exc:
                await manager.on_error(lease.id)
                raise RuntimeError(f"Gemini error: {exc}") from exc

        raise RuntimeError("Gemini: exhausted retries")

    def _build_body(self, messages: list[LLMMessage], system_prompt: str) -> bytes:
        contents = [
            {"role": "model" if m.role == "assistant" else "user",
             "parts": [{"text": m.content}]}
            for m in messages
        ]
        payload: dict = {"contents": contents}
        if system_prompt:
            payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
        return json.dumps(payload).encode()

    @staticmethod
    def _call(req: urllib.request.Request) -> str:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())

        # Prompt-level block
        feedback = data.get("promptFeedback", {})
        block_reason = feedback.get("blockReason")
        if block_reason:
            raise GeminiContentError(
                reason=f"prompt blocked by Gemini: {block_reason}",
                finish_reason="PROMPT_BLOCKED",
            )

        candidates = data.get("candidates", [])
        if not candidates:
            raise GeminiContentError(reason="Gemini returned no candidates")

        candidate = candidates[0]
        finish = candidate.get("finishReason", "STOP")
        safety = candidate.get("safetyRatings", [])
        content = candidate.get("content", {})
        parts = content.get("parts", [])

        if finish not in ("STOP", "MAX_TOKENS") or not parts:
            blocked = [
                f"{r['category'].replace('HARM_CATEGORY_', '')}: {r['probability']}"
                for r in safety
                if r.get("probability") not in ("NEGLIGIBLE", "LOW")
            ]
            detail = ", ".join(blocked) if blocked else finish
            logger.warning("Gemini blocked: finishReason=%s safety=%s", finish, safety)
            raise GeminiContentError(
                reason=f"response blocked: {detail}" if blocked else f"no content (finishReason={finish})",
                finish_reason=finish,
                safety_ratings=safety,
            )

        return parts[0].get("text", "")
