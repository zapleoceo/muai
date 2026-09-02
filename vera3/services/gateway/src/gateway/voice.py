"""POST /v1/voice/session — приём разговора с ноутбука.

Слушатель на Windows (vera-listener) пишет микрофон и системный звук,
распознаёт речь ЛОКАЛЬНО и присылает сюда расшифровку одной сессии.

Ключевое свойство: дословная расшифровка здесь НЕ сохраняется. Она нужна
только чтобы Вера один раз её осмыслила; в events уходит выжимка — с кем,
через что, о чём, решения, договорённости, цифры плюс цитаты там, где важна
формулировка. Сырой текст остаётся на ноутбуке и там же протухает.

Так же решается вопрос ключей: брокерский ключ живёт на сервере, ноутбуку он
не нужен — тот шлёт только текст под X-Internal-Secret.

Осмысление — `voice_distill`: длинная расшифровка сворачивается по окнам, а не
обрезается. Встреча длиннее предохранителя приходит частями с общим
`meeting_id`, и части связаны в метаданных.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow

from gateway.auth import check_internal_secret
from gateway.voice_distill import distill

log = logging.getLogger(__name__)
router = APIRouter()


class Utterance(BaseModel):
    at: float = Field(description="секунд от начала сессии")
    stream: Literal["mic", "system"]
    text: str
    # Микрофон слышит динамики, поэтому часть реплик дорожки mic — это голос
    # собеседника. Слушатель их помечает, но не выбрасывает: в один кусок
    # попадает и эхо, и слова владельца. Старые слушатели поля не шлют — отсюда
    # дефолт, и от этого ничего не ломается.
    echo: bool = False
    # Кто именно сказал. Дорожка `system` смешивает всех удалённых участников
    # в один поток, и без этого поля в созвоне на пятерых видно только «не
    # владелец». Слушатель разделяет их по голосу и, где приложение назвало
    # собеседника, подставляет настоящее имя. None — имя не установлено:
    # старый слушатель, дорожка микрофона или голос, который не с чем сверить.
    speaker: str | None = None


class VoiceSession(BaseModel):
    started_at: datetime
    ended_at: datetime
    app: str | None = None            # zoom.exe, telegram.exe, chrome.exe
    window_title: str | None = None   # часто содержит имя собеседника
    device_hint: str | None = None    # наушники/динамики — для диагностики
    # Части одной длинной встречи несут общий meeting_id: предохранитель на
    # ноутбуке режет разговор, но связь между половинами должна остаться.
    # Старые файлы из офлайн-очереди этих полей не несут — отсюда дефолты.
    meeting_id: str | None = None
    part: int = 1
    utterances: list[Utterance] = Field(min_length=1)


class VoiceSessionResult(BaseModel):
    ok: bool
    event_id: int | None
    deduped: bool = False
    summary: str | None = None


def voices_of(utterances: list[Utterance]) -> list[str]:
    """Имена опознанных голосов, по одному разу и в устойчивом порядке.

    Одно место на обоих потребителей — стенограмму и метаданные события:
    две копии одного выражения разъехались бы при первой же правке.
    """
    return sorted({u.speaker for u in utterances if u.speaker})


def transcript_record(utterances: list[Utterance]) -> dict[str, Any]:
    """Стенограмма для `events.content_extra` — дословно, с автором реплики.

    `stream` и есть авторство на сегодня: mic — владелец, system — удалённая
    сторона. Метки конкретных говорящих внутри системной дорожки появятся
    отдельно; формат заранее оставляет под них место в каждой реплике.
    """
    return {
        "kind": "voice_transcript",
        "chars": sum(len(u.text) for u in utterances),
        "echoes": sum(1 for u in utterances if u.echo),
        "speakers": voices_of(utterances),
        "utterances": [
            # `echo` и `speaker` пишем только когда они есть: ключ у каждой
            # реплики раздувал бы jsonb без пользы.
            {"at": round(u.at, 2), "stream": u.stream, "text": u.text,
             **({"echo": True} if u.echo else {}),
             **({"speaker": u.speaker} if u.speaker else {})}
            for u in utterances
        ],
    }


def body_text(d: dict, app: str | None, title: str | None) -> str:
    """Человекочитаемое тело события — его и увидит поиск по мозгу."""
    parts = [str(d.get("summary", "")).strip()]
    for label, key in (("Участники", "counterparts"), ("Темы", "topics"),
                       ("Решения", "decisions"), ("Договорённости", "commitments"),
                       ("Числа и сроки", "numbers")):
        vals = [str(v).strip() for v in (d.get(key) or []) if str(v).strip()]
        if vals:
            parts.append(f"{label}: " + "; ".join(vals))
    steps = [str(v).strip() for v in (d.get("outline") or []) if str(v).strip()]
    if steps:
        parts.append("Ход разговора:\n" + "\n".join(f"— {s}" for s in steps))
    quotes = [str(q).strip() for q in (d.get("key_quotes") or []) if str(q).strip()]
    if quotes:
        parts.append("Цитаты:\n" + "\n".join(f"— {q}" for q in quotes))
    where = " / ".join(x for x in (app, title) if x)
    if where:
        parts.append(f"Где: {where}")
    return "\n\n".join(p for p in parts if p)


@router.post("/v1/voice/session", response_model=VoiceSessionResult)
async def ingest_voice_session(
    body: VoiceSession,
    x_internal_secret: str | None = Header(default=None),
) -> VoiceSessionResult:
    check_internal_secret(x_internal_secret)

    # Осмысление получает разговор БЕЗ эха: задвоенные реплики и сбивают
    # выжимку, и приписывают слова собеседника владельцу. Стенограмма ниже
    # берёт полный список — что выброшено здесь, там сохранено.
    # `or body.utterances` — защита контракта, а не наблюдаемый случай: живой
    # слушатель помечает только дорожку mic и только при наличии непомеченных
    # реплик из loopback, поэтому хоть одна чистая остаётся всегда. Но сервер
    # не обязан верить клиенту на слово, а пустая выжимка хуже неточной.
    for_summary = [u for u in body.utterances if not u.echo] or body.utterances
    distilled, report = await distill(for_summary, app=body.app,
                                      title=body.window_title)
    if not report["transcript_chars"]:
        return VoiceSessionResult(ok=False, event_id=None)

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
                content_text=body_text(distilled, body.app, body.window_title)[:8000],
                # Дословная стенограмма — рядом с событием, но НЕ в content_text:
                # вектор строится только по выжимке (brain-triage/worker.py), и
                # сотня обрывков «ага, давай» не должна перебивать её в поиске.
                # Хранить дословное обязательно: выжимка сжимает разговор в
                # тридцать раз (замер: 66 445 символов при потолке 8 000), а
                # звук не хранится вообще — что выброшено, того больше нигде нет.
                content_extra=transcript_record(body.utterances),
                occurred_at=body.started_at.astimezone(timezone.utc).replace(tzinfo=None),
                metadata_={
                    "app": body.app,
                    "window_title": body.window_title,
                    "device_hint": body.device_hint,
                    "duration_s": dur,
                    "utterances": len(body.utterances),
                    "utterances_echo": sum(1 for u in body.utterances if u.echo),
                    # Имена говорящих — рядом с counterparts из выжимки, но
                    # это РАЗНОЕ: там кого назвала модель по тексту, здесь
                    # кого опознали по голосу.
                    "voices": voices_of(body.utterances),
                    "counterparts": distilled.get("counterparts") or [],
                    "topics": distilled.get("topics") or [],
                    "meeting_id": body.meeting_id or src_id,
                    "part": body.part,
                    "author_role": "self",
                    "author_label": "Я",
                    **report,
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

    log.info("voice: сессия %s -> event=%s (%dс, %d реплик, %d симв., окон %d%s)",
             src_id, event_id, dur, len(body.utterances),
             report["transcript_chars"], report["windows"],
             ", ХВОСТ ОБРЕЗАН" if report["truncated"] else "")
    return VoiceSessionResult(ok=True, event_id=event_id,
                              summary=distilled.get("summary"))
