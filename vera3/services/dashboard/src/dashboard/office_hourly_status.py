"""Pure helpers of the hourly report: status info input, evidence text, reminder checkpoint."""
from __future__ import annotations

from datetime import datetime, timedelta

from vera_shared.db.models_room import (
    RoomTaskEventRow,
    RoomTaskQuestionRow,
    RoomTaskRow,
)
from vera_shared.room.attention import ANSWERED, PAUSED, Attention

from dashboard.office_status import StatusInfo, bound_reason_of, describe_status

REMINDER = timedelta(hours=6)


def status_info_of(row: RoomTaskRow, att: Attention, question: RoomTaskQuestionRow | None,
                   now: datetime, control: str | None = None,
                   event_at: datetime | None = None,
                   control_event: RoomTaskEventRow | None = None,
                   control_next: RoomTaskEventRow | None = None) -> StatusInfo:
    return describe_status(
        row, now, has_open_question=question is not None, answered=att.state == ANSWERED,
        control="paused" if att.state == PAUSED else control, event_at=event_at,
        control_text=control_event.text if control_event else None,
        control_agent=control_event.agent if control_event else None,
        control_data=control_event.data if control_event else None,
        control_bound_reason=bound_reason_of(control_event, control_next),
        control_at=control_event.at if control_event else None)


def status_of(row: RoomTaskRow, att: Attention, question: RoomTaskQuestionRow | None,
              now: datetime, control: str | None = None) -> str:
    return status_info_of(row, att, question, now, control).status


def evidence_of(event: RoomTaskEventRow | None, now: datetime) -> str:
    if event is None:
        return "Сообщения о результате в Room нет; внешнее подтверждение неизвестно"
    age = now - event.at
    if age > timedelta(hours=24):
        return "Сообщение о результате старше суток; внешнее подтверждение неизвестно"
    return "Сообщение о результате с отметкой времени; внешнее подтверждение неизвестно"


def next_question_reminder(question: RoomTaskQuestionRow, now: datetime) -> datetime:
    """Next six-hour display checkpoint; an answered question is never passed here."""
    elapsed = max((now - question.asked_at) // REMINDER, 0)
    return question.asked_at + (elapsed + 1) * REMINDER
