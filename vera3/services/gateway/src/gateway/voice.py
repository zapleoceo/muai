"""POST /v1/voice/session — приём разговора с ноутбука.

Слушатель на Windows (vera-listener) пишет микрофон и системный звук,
распознаёт речь ЛОКАЛЬНО и присылает сюда расшифровку одной сессии.

Ключевое свойство: дословная расшифровка здесь НЕ сохраняется. Она нужна
только чтобы Вера один раз её осмыслила; в events уходит выжимка — с кем,
через что, о чём, решения, договорённости, цифры плюс цитаты там, где важна
формулировка. Сырой текст остаётся на ноутбуке и там же протухает.

Так же решается вопрос ключей: брокерский ключ живёт на сервере, ноутбуку он
не нужен — тот шлёт только текст под X-Internal-Secret.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.llm.client import LLMCallFailed, chat_async

from gateway.auth import check_internal_secret

log = logging.getLogger(__name__)
router = APIRouter()

MAX_TRANSCRIPT_CHARS = 60_000


class Utterance(BaseModel):
    at: float = Field(description="секунд от начала сессии")
    stream: Literal["mic", "system"]
    text: str


class VoiceSession(BaseModel):
    started_at: datetime
    ended_at: datetime
    app: str | None = None            # zoom.exe, telegram.exe, chrome.exe
    window_title: str | None = None   # часто содержит имя собеседника
    device_hint: str | None = None    # наушники/динамики — для диагностики
    utterances: list[Utterance] = Field(min_length=1)


class VoiceSessionResult(BaseModel):
    ok: bool
    event_id: int | None
    deduped: bool = False
    summary: str | None = None


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
                "decisions": {"type": "array", "items": {"type": "string"}},
                "commitments": {"type": "array", "items": {"type": "string"}},
                "numbers": {"type": "array", "items": {"type": "string"}},
                "key_quotes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "counterparts", "topics", "decisions",
                         "commitments", "numbers", "key_quotes"],
            "additionalProperties": False,
        },
    },
}

_PROMPT = """Ты — память Димы. Ниже расшифровка одного разговора.

[я] — говорит Дима или человек рядом с ним (микрофон).
[собеседник] — звук из приложения, то есть удалённая сторона.

Приложение: {app}
Заголовок окна: {title}

Верни СТРОГО JSON по схеме:
- summary — 2-5 предложений: с кем говорили, о чём, чем кончилось;
- counterparts — имена участников, если названы в разговоре или в заголовке;
- topics — темы одним-двумя словами;
- decisions — что решили (пусто, если не решали);
- commitments — кто что кому пообещал, со сроками если названы;
- numbers — суммы, даты, сроки, количества дословно;
- key_quotes — до 5 цитат, где важна ИМЕННО формулировка.

Не выдумывай: чего в тексте нет — того нет. Пустой список лучше догадки.

--- расшифровка ---
{transcript}"""


def _render(utterances: list[Utterance]) -> str:
    return "\n".join(
        f"[{'я' if u.stream == 'mic' else 'собеседник'}] {u.text.strip()}"
        for u in utterances if u.text.strip()
    )


def _body(d: dict[str, Any], app: str | None, title: str | None) -> str:
    """Человекочитаемое тело события — его и увидит поиск по мозгу."""
    parts = [str(d.get("summary", "")).strip()]
    for label, key in (("Участники", "counterparts"), ("Темы", "topics"),
                       ("Решения", "decisions"), ("Договорённости", "commitments"),
                       ("Числа и сроки", "numbers")):
        vals = [str(v).strip() for v in (d.get(key) or []) if str(v).strip()]
        if vals:
            parts.append(f"{label}: " + "; ".join(vals))
    quotes = [str(q).strip() for q in (d.get("key_quotes") or []) if str(q).strip()]
    if quotes:
        parts.append("Цитаты:\n" + "\n".join(f"— {q}" for q in quotes))
    where = " / ".join(x for x in (app, title) if x)
    if where:
        parts.append(f"Где: {where}")
    return "\n\n".join(p for p in parts if p)


_EMPTY = {"summary": "(разговор не удалось осмыслить — сохранён только факт)",
          "counterparts": [], "topics": [], "decisions": [],
          "commitments": [], "numbers": [], "key_quotes": []}


@router.post("/v1/voice/session", response_model=VoiceSessionResult)
async def ingest_voice_session(
    body: VoiceSession,
    x_internal_secret: str | None = Header(default=None),
) -> VoiceSessionResult:
    check_internal_secret(x_internal_secret)

    transcript = _render(body.utterances)[:MAX_TRANSCRIPT_CHARS]
    if not transcript:
        return VoiceSessionResult(ok=False, event_id=None)

    try:
        raw, _meta = await chat_async(
            messages=[{"role": "user", "content": _PROMPT.format(
                app=body.app or "неизвестно",
                title=body.window_title or "нет",
                transcript=transcript)}],
            capability="chat:smart",
            response_format=VOICE_JSON_SCHEMA,
            max_tokens=1200,
            temperature=0.2,
            workflow="voice_session",
        )
        distilled = json.loads(raw)
    except (LLMCallFailed, json.JSONDecodeError) as e:
        # Осмыслить не вышло — событие всё равно должно попасть в память,
        # иначе разговор потеряется молча.
        log.warning("voice: distill failed (%s) — сохраняю без выжимки", e)
        distilled = dict(_EMPTY)

    # Идентичность сессии — время начала + контекст: повторная отправка той же
    # сессии (ретрай из офлайн-очереди) не задвоит событие.
    sig = f"{body.started_at.isoformat()}|{body.app}|{body.window_title}"
    src_id = "voice:" + hashlib.sha1(sig.encode()).hexdigest()[:16]
    dur = max(0, int((body.ended_at - body.started_at).total_seconds()))

    async with get_session() as s:
        stmt = (
            pg_insert(EventRow)
            .values(
                source="voice",
                source_event_id=src_id,
                account="laptop",
                category="conversation",
                content_text=_body(distilled, body.app, body.window_title)[:8000],
                occurred_at=body.started_at.astimezone(timezone.utc).replace(tzinfo=None),
                metadata_={
                    "app": body.app,
                    "window_title": body.window_title,
                    "device_hint": body.device_hint,
                    "duration_s": dur,
                    "utterances": len(body.utterances),
                    "counterparts": distilled.get("counterparts") or [],
                    "topics": distilled.get("topics") or [],
                    "author_role": "self",
                    "author_label": "Я",
                },
                triage_status="pending",
            )
            .on_conflict_do_nothing(index_elements=["source", "source_event_id"])
            .returning(EventRow.id)
        )
        event_id = (await s.execute(stmt)).scalar_one_or_none()

    if event_id is None:
        log.info("voice: сессия уже была (%s)", src_id)
        return VoiceSessionResult(ok=True, event_id=None, deduped=True)

    log.info("voice: сессия %s -> event=%s (%dс, %d реплик)",
             src_id, event_id, dur, len(body.utterances))
    return VoiceSessionResult(ok=True, event_id=event_id,
                              summary=distilled.get("summary"))
