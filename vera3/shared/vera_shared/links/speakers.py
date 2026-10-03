"""Карта голосов созвона (миграция 044): владелец или агент называет «Собеседник 2».

`kind='event'` — ярлык внутри одного созвона; `kind='voiceprint'` — id отпечатка (если
слушатель его присылает): назвали один раз — узнаётся в каждом созвоне при пересчёте.
Запись идёт в сессии вызывающего (правка и журнал — одна транзакция) и сразу правит
связь `participant`/`manual` этого ярлыка в `event_entities`, поэтому отдельный
пересчёт события не нужен; тот же результат даёт и полный пересчёт индекса.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.models import EventRow
from vera_shared.db.models_links import EventEntityRow, VoiceSpeakerMapRow
from vera_shared.graph import edit as graph_edit
from vera_shared.links.model import MANUAL, PARTICIPANT

EVENT_KIND, VOICEPRINT_KIND = "event", "voiceprint"


class SpeakerError(ValueError):
    """Ярлык или событие непригодны для карты голосов."""


async def _row(s: AsyncSession, kind: str, key: str) -> VoiceSpeakerMapRow | None:
    return (await s.execute(select(VoiceSpeakerMapRow).where(
        VoiceSpeakerMapRow.kind == kind, VoiceSpeakerMapRow.key == key))).scalar_one_or_none()


async def put_speaker(s: AsyncSession, event_id: int, label: str,
                      entity_id: int | None) -> int | None:
    """Назвать говорящего `label` созвона `event_id` сущностью (None — снять название).
    → прежняя сущность ярлыка."""
    label = label.strip()
    if not label:
        raise SpeakerError("label must not be empty")
    event = await s.get(EventRow, event_id)
    if event is None or event.source != "voice":
        raise SpeakerError(f"event {event_id} is not a voice call")
    key = f"{event_id}:{label}"
    row = await _row(s, EVENT_KIND, key)
    previous = row.entity_id if row else None
    await s.execute(delete(EventEntityRow).where(
        EventEntityRow.event_id == event_id, EventEntityRow.role == PARTICIPANT,
        EventEntityRow.token == label[:120], EventEntityRow.source_of_link == MANUAL))
    if entity_id is None:
        if row is not None:
            await s.delete(row)
        return previous
    await graph_edit.current_name(s, entity_id)
    if row is None:
        s.add(VoiceSpeakerMapRow(kind=EVENT_KIND, key=key, entity_id=entity_id))
    else:
        row.entity_id = entity_id
    s.add(EventEntityRow(event_id=event_id, entity_id=entity_id, role=PARTICIPANT,
                         token=label[:120], source_of_link=MANUAL, confidence=1.0,
                         span={"speaker": label}))
    return previous


async def put_voiceprint(s: AsyncSession, voiceprint_id: str, entity_id: int) -> int | None:
    """Назвать отпечаток голоса сущностью; → прежняя сущность. Связи созвонов, где он
    звучит, обновит ближайший пересчёт индекса (`backfill_event_links.py --reset`)."""
    voiceprint_id = voiceprint_id.strip()
    if not voiceprint_id:
        raise SpeakerError("voiceprint_id must not be empty")
    await graph_edit.current_name(s, entity_id)
    row = await _row(s, VOICEPRINT_KIND, voiceprint_id)
    previous = row.entity_id if row else None
    if row is None:
        s.add(VoiceSpeakerMapRow(kind=VOICEPRINT_KIND, key=voiceprint_id, entity_id=entity_id))
    else:
        row.entity_id = entity_id
    return previous
