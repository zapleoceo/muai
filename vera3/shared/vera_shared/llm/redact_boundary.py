"""Маскирование секретов на границе «Vera → внешний LLM/embedding-провайдер».

Сохранённые события остаются сырыми (FTS, точные совпадения идут по ним);
маскируется только то, что уходит наружу. Копии, не мутация входа.
"""
from __future__ import annotations

from typing import Any

from vera_shared.redact_view import redact_view


def _redact_part(part: Any) -> Any:
    is_text = isinstance(part, dict) and part.get("type") in ("text", "input_text")
    if is_text and isinstance(part.get("text"), str):
        return {**part, "text": redact_view(part["text"])}
    return part


def _redact_tool_calls(calls: Any) -> Any:
    if not isinstance(calls, list):
        return calls
    out = []
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
            call = {**call, "function": {**fn, "arguments": redact_view(fn["arguments"])}}
        out.append(call)
    return out


def redact_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Копия messages с замаскированными текстовыми частями; image/audio/data: не трогаем."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        new = dict(msg)
        content = msg.get("content")
        if isinstance(content, str):
            new["content"] = redact_view(content)
        elif isinstance(content, list):
            new["content"] = [_redact_part(p) for p in content]
        if "tool_calls" in msg:
            new["tool_calls"] = _redact_tool_calls(msg["tool_calls"])
        out.append(new)
    return out


def redact_embed_input(text: str | list[str]) -> str | list[str]:
    if isinstance(text, str):
        return redact_view(text)
    return [redact_view(t) for t in text]
