import asyncio
import base64
import json

import httpx

from app.services.tokens import get_token_manager

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
_MAX_RETRIES = 3


def _inline_data_part(*, mime_type: str, data: bytes) -> dict:
    return {"inlineData": {"mimeType": str(mime_type), "data": base64.b64encode(data).decode()}}


async def transcribe_audio(*, data: bytes, mime_type: str, language: str = "ru") -> str:
    if not data:
        return ""

    mgr = get_token_manager()

    prompt = (
        "Сделай точную транскрипцию аудио. "
        "Верни только текст, без пояснений и без форматирования. "
        f"Язык: {language}."
    )

    body = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}, _inline_data_part(mime_type=mime_type, data=data)]}
        ]
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
        for attempt in range(_MAX_RETRIES):
            lease = await mgr.next_token("chat", provider="gemini")
            if not lease:
                raise RuntimeError("No Gemini tokens configured. Add one at /api/admin/tokens.")
            url = f"{_BASE_URL}?key={lease.token}"
            try:
                resp = await client.post(url, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                await mgr.on_error(lease.id)
                raise RuntimeError(f"Gemini network error: {str(exc)[:200]}") from exc

            if resp.status_code == 429:
                await mgr.on_rate_limit(lease.id)
                if attempt == _MAX_RETRIES - 1:
                    raise RuntimeError("All Gemini tokens rate-limited")
                continue

            if resp.status_code >= 400:
                await mgr.on_error(lease.id)
                raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:200]}")

            data_json = resp.json()
            candidates = data_json.get("candidates") or []
            if not candidates:
                return ""
            content = (candidates[0].get("content") or {})
            parts = content.get("parts") or []
            if not parts:
                return ""
            text = str(parts[0].get("text") or "")
            return text.strip()

    return ""
