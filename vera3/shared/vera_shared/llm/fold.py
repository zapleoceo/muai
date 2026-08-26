"""Свёртка длинного текста в структурированную выжимку: карта-свёртка, не обрезка.

Механика одна на все длинные тексты: строки режутся на окна по границам
смысловых единиц, каждое окно осмысляется отдельно, второй проход сливает
частичные выжимки в одну. Разное — только промпты и набор полей, и это `FoldSpec`.

Появилось выносом из `gateway.voice_distill`, когда та же свёртка понадобилась
сессиям Claude Code. Копия промптов и склейки во втором месте разъехалась бы с
первым при первой же правке.

Два потолка остаются честными: `max_windows` (аварийный, при нём
`truncated=True` и предупреждение в лог) и `part_tokens` в множителе на слияние.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from vera_shared.llm.client import LLMCallFailed, chat_async

log = logging.getLogger(__name__)

#: Заметно меньше контекста модели: на большом объёме выжимка «размазывает»
#: середину даже там, где всё физически влезло.
DEFAULT_WINDOW_CHARS = 35_000
DEFAULT_MAX_WINDOWS = 12


@dataclass(frozen=True)
class FoldSpec:
    """Что именно сворачиваем: поля, промпты, потолки.

    `fields[0]` — всегда строковое `summary`, остальные — списки строк. Схема
    для модели строится отсюда же, чтобы поля в промпте и в схеме не разошлись.
    """

    name: str
    fields: tuple[str, ...]
    part_prompt: str
    merge_prompt: str
    empty_summary: str
    window_chars: int = DEFAULT_WINDOW_CHARS
    max_windows: int = DEFAULT_MAX_WINDOWS
    part_tokens: int = 1200

    def __post_init__(self) -> None:
        if not self.fields or self.fields[0] != "summary":
            raise ValueError("первое поле FoldSpec обязано быть summary")

    @property
    def list_fields(self) -> tuple[str, ...]:
        return self.fields[1:]

    @property
    def json_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {"summary": {"type": "string"}}
        for field in self.list_fields:
            properties[field] = {"type": "array", "items": {"type": "string"}}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": self.name,
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(self.fields),
                    "additionalProperties": False,
                },
            },
        }

    @property
    def empty(self) -> dict[str, Any]:
        return {"summary": self.empty_summary,
                **{field: [] for field in self.list_fields}}


def windows(lines: list[str], *, limit: int = DEFAULT_WINDOW_CHARS,
            max_windows: int = DEFAULT_MAX_WINDOWS) -> tuple[list[str], bool]:
    """Строки → окна не больше `limit` символов. Резать только по границам строк.

    Второй элемент — упёрлись ли в аварийный потолок (тогда хвост потерян и об
    этом обязаны знать и лог, и метаданные события).
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        # Строка длиннее окна — кладём как есть: рвать её посередине хуже,
        # чем один раз превысить лимит.
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    if len(chunks) > max_windows:
        return chunks[:max_windows], True
    return chunks, False


async def _one(prompt: str, spec: FoldSpec, *, max_tokens: int) -> dict[str, Any] | None:
    try:
        raw, _meta = await chat_async(
            messages=[{"role": "user", "content": prompt}],
            capability="chat:smart", response_format=spec.json_schema,
            max_tokens=max_tokens, temperature=0.2, workflow=spec.name,
        )
        return json.loads(raw)
    except (LLMCallFailed, json.JSONDecodeError) as e:
        log.warning("%s: осмыслить не вышло (%s)", spec.name, e)
        return None


def stitch(parts: list[dict[str, Any]], spec: FoldSpec) -> dict[str, Any]:
    """Механическая склейка — страховка, если слить моделью не удалось."""
    merged: dict[str, Any] = {"summary": " ".join(
        str(p.get("summary") or "").strip() for p in parts if p.get("summary"))}
    for field in spec.list_fields:
        seen: list[str] = []
        for part in parts:
            for value in part.get(field) or []:
                text = str(value).strip()
                if text and text not in seen:
                    seen.append(text)
        merged[field] = seen
    return merged


async def fold(lines: list[str], spec: FoldSpec,
               context: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Строки → (выжимка, отчёт о работе). Никогда не бросает.

    Отчёт идёт в metadata события, чтобы потеря или частичный провал были
    видны, а не молча растворились в тексте выжимки.
    """
    chars = sum(len(line) + 1 for line in lines)
    chunks, truncated = windows(lines, limit=spec.window_chars,
                                max_windows=spec.max_windows)
    report = {"transcript_chars": chars, "windows": len(chunks),
              "truncated": truncated, "distilled": True}
    if truncated:
        log.warning("%s: текст %d символов не влез в %d окон — хвост не осмыслен",
                    spec.name, chars, spec.max_windows)
    if not chunks:
        return dict(spec.empty), {**report, "distilled": False}

    if len(chunks) == 1:
        result = await _one(
            spec.part_prompt.format(scope="текст", transcript=chunks[0], **context),
            spec, max_tokens=spec.part_tokens)
        if result is None:
            return dict(spec.empty), {**report, "distilled": False}
        return result, report

    parts: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        got = await _one(
            spec.part_prompt.format(scope=f"часть {index} из {len(chunks)}",
                                    transcript=chunk, **context),
            spec, max_tokens=spec.part_tokens)
        if got is not None:
            parts.append(got)
    if not parts:
        return dict(spec.empty), {**report, "distilled": False}

    merged = await _one(
        spec.merge_prompt.format(
            parts=json.dumps(parts, ensure_ascii=False, indent=1), **context),
        spec, max_tokens=min(spec.part_tokens + 400 * len(parts), 4000))
    if merged is None:
        # Слить моделью не вышло — событие всё равно должно быть полным.
        return stitch(parts, spec), {**report, "merged": "mechanical"}
    return merged, {**report, "merged": "llm", "parts": len(parts)}
