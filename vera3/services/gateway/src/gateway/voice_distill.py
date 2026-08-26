"""Осмысление разговора: расшифровка → выжимка. Свёртка, а не обрезка.

До 2026-08-26 здесь стоял срез `[:60_000]` без лога и без отметки. По
арифметике (≈12.6 символа на секунду речи с префиксами реплик) это ≈80 минут
суммарной речи по обеим дорожкам, тогда как предохранитель на ноутбуке отдаёт
сессии до 120 минут: клиент по конструкции способен прислать больше, чем
сервер был готов прочитать. Терялся ХВОСТ — та часть, где «значит, договорились
так», сроки и суммы.

Теперь длинная расшифровка режется по границам реплик на окна, каждое окно
осмысляется отдельно, и второй проход сливает частичные выжимки в одну. Один
разговор остаётся одним событием, хвост не теряется, и качество не проседает
от того, что модели скормили сто тысяч символов одним сообщением.

Осталось два потолка, и оба честные: `MAX_WINDOWS` (аварийный, при нём
`truncated=True` и предупреждение в лог) и число окон в множителе `max_tokens`.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from vera_shared.llm.client import LLMCallFailed, chat_async

log = logging.getLogger(__name__)

#: Размер окна. Заметно меньше контекста модели: на большом объёме выжимка
#: «размазывает» середину даже там, где всё физически влезло.
WINDOW_CHARS = 35_000
#: Аварийный потолок ≈ 9 часов речи. Сессия с ноутбука столько не бывает
#: (предохранитель), поэтому упереться сюда — повод разбираться, а не молчать.
MAX_WINDOWS = 12

FIELDS = ("summary", "counterparts", "topics", "outline",
          "decisions", "commitments", "numbers", "key_quotes")

_LIST_FIELDS = tuple(f for f in FIELDS if f != "summary")

VOICE_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "voice_session",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "counterparts": {"type": "array", "items": {"type": "string"}},
                "topics": {"type": "array", "items": {"type": "string"}},
                "outline": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "commitments": {"type": "array", "items": {"type": "string"}},
                "numbers": {"type": "array", "items": {"type": "string"}},
                "key_quotes": {"type": "array", "items": {"type": "string"}},
            },
            "required": list(FIELDS),
            "additionalProperties": False,
        },
    },
}

EMPTY: dict[str, Any] = {
    "summary": "(разговор не удалось осмыслить — сохранён только факт)",
    **{f: [] for f in _LIST_FIELDS},
}

_PART = """Ты — память Димы. Ниже {scope} одного разговора.

[я] — говорит Дима или человек рядом с ним (микрофон).
[собеседник] — звук из приложения, то есть удалённая сторона.

Приложение: {app}
Заголовок окна: {title}

Верни СТРОГО JSON по схеме:
- summary — 2-5 предложений: с кем говорили, о чём, чем кончилось;
- counterparts — имена участников, если названы в разговоре или в заголовке;
- topics — темы одним-двумя словами;
- outline — ход разговора по порядку, одна строка на смысловой шаг;
- decisions — что решили (пусто, если не решали);
- commitments — кто что кому пообещал, со сроками если названы;
- numbers — суммы, даты, сроки, количества дословно;
- key_quotes — до 5 цитат, где важна ИМЕННО формулировка.

Не выдумывай: чего в тексте нет — того нет. Пустой список лучше догадки.

--- {scope} ---
{transcript}"""

_MERGE = """Ты — память Димы. Длинный разговор осмыслен по частям, части идут по
порядку. Собери из них ОДНУ выжимку того же формата.

Приложение: {app}
Заголовок окна: {title}

Правила сборки:
- summary — про разговор ЦЕЛИКОМ, 3-7 предложений, а не склейка частей;
- outline — общий ход по порядку частей, без повторов;
- остальные списки — объединить и убрать дубли, порядок сохранить;
- решения и договорённости из ПОЗДНИХ частей важнее: если ранняя часть решала
  одно, а поздняя переиграла — в итоге поздняя, но упомяни, что меняли;
- ничего не додумывай: что не названо ни в одной части, того нет.

--- части ---
{parts}"""


def render(utterances: list[Any]) -> list[str]:
    """Реплики → строки расшифровки. Порядок как пришёл, пустые выброшены."""
    lines = []
    for u in utterances:
        text = (u.text if hasattr(u, "text") else u.get("text", "")).strip()
        if not text:
            continue
        stream = u.stream if hasattr(u, "stream") else u.get("stream", "mic")
        lines.append(f"[{'я' if stream == 'mic' else 'собеседник'}] {text}")
    return lines


def windows(lines: list[str], *, limit: int = WINDOW_CHARS,
            max_windows: int = MAX_WINDOWS) -> tuple[list[str], bool]:
    """Строки → окна не больше `limit` символов. Резать только по границам реплик.

    Второй элемент — упёрлись ли в аварийный потолок (тогда хвост потерян и об
    этом обязаны знать и лог, и метаданные события).
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        # Реплика длиннее окна — кладём как есть: рвать её посередине хуже,
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


def _ask(scope: str, transcript: str, app: str | None, title: str | None) -> str:
    return _PART.format(scope=scope, transcript=transcript,
                        app=app or "неизвестно", title=title or "нет")


async def _one(prompt: str, *, max_tokens: int) -> dict[str, Any] | None:
    try:
        raw, _meta = await chat_async(
            messages=[{"role": "user", "content": prompt}],
            capability="chat:smart", response_format=VOICE_JSON_SCHEMA,
            max_tokens=max_tokens, temperature=0.2, workflow="voice_session",
        )
        return json.loads(raw)
    except (LLMCallFailed, json.JSONDecodeError) as e:
        log.warning("voice: осмыслить не вышло (%s)", e)
        return None


def _stitch(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Механическая склейка — страховка, если слить моделью не удалось."""
    merged: dict[str, Any] = {"summary": " ".join(
        str(p.get("summary") or "").strip() for p in parts if p.get("summary"))}
    for field in _LIST_FIELDS:
        seen: list[str] = []
        for part in parts:
            for value in part.get(field) or []:
                text = str(value).strip()
                if text and text not in seen:
                    seen.append(text)
        merged[field] = seen
    return merged


async def distill(utterances: list[Any], *, app: str | None,
                  title: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Расшифровка → (выжимка, отчёт о работе). Никогда не бросает.

    Отчёт идёт в metadata события, чтобы потеря или частичный провал были
    видны, а не молча растворились в тексте выжимки.
    """
    lines = render(utterances)
    chars = sum(len(line) + 1 for line in lines)
    chunks, truncated = windows(lines)
    report = {"transcript_chars": chars, "windows": len(chunks),
              "truncated": truncated, "distilled": True}
    if truncated:
        log.warning("voice: расшифровка %d символов не влезла в %d окон — "
                    "хвост не осмыслен", chars, MAX_WINDOWS)
    if not chunks:
        return dict(EMPTY), {**report, "distilled": False}

    if len(chunks) == 1:
        result = await _one(_ask("расшифровка", chunks[0], app, title),
                            max_tokens=1200)
        if result is None:
            return dict(EMPTY), {**report, "distilled": False}
        return result, report

    parts: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        got = await _one(
            _ask(f"часть {index} из {len(chunks)} расшифровки", chunk, app, title),
            max_tokens=1200)
        if got is not None:
            parts.append(got)
    if not parts:
        return dict(EMPTY), {**report, "distilled": False}

    merged = await _one(
        _MERGE.format(app=app or "неизвестно", title=title or "нет",
                      parts=json.dumps(parts, ensure_ascii=False, indent=1)),
        max_tokens=min(1200 + 400 * len(parts), 4000))
    if merged is None:
        # Слить моделью не вышло — событие всё равно должно быть полным.
        return _stitch(parts), {**report, "merged": "mechanical"}
    return merged, {**report, "merged": "llm", "parts": len(parts)}
