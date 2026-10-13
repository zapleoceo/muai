"""Маскирование секретов на границе «Vera → внешний LLM/embedding-провайдер».

Сохранённые события остаются сырыми (FTS, точные совпадения идут по ним);
маскируется только то, что уходит наружу. Копии, не мутация входа.
"""
from __future__ import annotations

from typing import Any

from vera_shared.redact_view import redact_view


def _redact_part(part: Any) -> Any:
    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
        return {**part, "text": redact_view(part["text"])}
    return part


def redact_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Копия messages с замаскированными текстовыми частями; image/audio/data: не трогаем."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            out.append({**msg, "content": redact_view(content)})
        elif isinstance(content, list):
            out.append({**msg, "content": [_redact_part(p) for p in content]})
        else:
            out.append(msg)
    return out


def redact_embed_input(text: str | list[str]) -> str | list[str]:
    if isinstance(text, str):
        return redact_view(text)
    return [redact_view(t) for t in text]
