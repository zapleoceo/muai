"""Каноническая schema события (sources → gateway → processing → storage).

Каждый источник нормализует свой формат в RawEvent. Это **единственный**
shape с которым работают brain workers и storage.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from vera_shared.timeutil import utc_naive_now

# Тип сигнала который Вера может найти внутри события
SignalType = Literal[
    "task",     # требует действия
    "event",    # календарное событие, ДР, встреча
    "news",     # информация без действия
    "offer",    # коммерческое предложение
    "question", # вопрос требующий ответа
    "decision", # уже принятое решение, для записи
    "anomaly",  # что-то необычное
]


class Signal(BaseModel):
    """Извлечённый из события сигнал."""

    type: SignalType
    summary: str = Field(min_length=1, max_length=500)
    date: datetime | None = None
    metadata: dict[str, Any] | None = None


class EntityHint(BaseModel):
    """Хинт о упомянутой сущности (от ingestor или triage)."""

    type: Literal["person", "organization", "project", "place", "account", "topic"]
    identifier: str = Field(min_length=1)  # canonical ID (email, @handle, etc.)
    name: str | None = None  # human-readable
    extra: dict[str, Any] | None = None


class TriageMetadata(BaseModel):
    """Результат AI-триажа события."""

    importance: int = Field(ge=0, le=100)
    topics: list[str] = Field(default_factory=list)
    people_mentioned: list[str] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    active_topic_matches: list[dict[str, Any]] = Field(default_factory=list)
    needs_action: bool = False
    # Единственное место, где utcnow передавалась как CALLABLE, а не вызывалась.
    # Поэтому её не видел ни grep по `utcnow()`, ни ruff DTZ003 — нашлась она
    # только по DeprecationWarning из pydantic в прогоне тестов.
    triaged_at: datetime = Field(default_factory=utc_naive_now)
    triaged_by_provider: str | None = None
    triaged_by_model: str | None = None


class RawEvent(BaseModel):
    """Каноническое событие. Создаётся ingestor'ом, шлётся в gateway.

    После приёма получает дополнительные поля: id (DB), embedding,
    triage_metadata, graphiti_episode_uuid (если в граф попало).
    """

    # Обязательное
    source: str = Field(min_length=1, description="gmail, telegram, instagram, ...")
    source_event_id: str = Field(min_length=1, description="уникальный ID в источнике")
    occurred_at: datetime
    content_text: str = Field(default="", description="текст события (может быть пуст)")

    # Опционально
    account: str | None = Field(default=None, description="какой аккаунт источника")
    category: str = Field(default="generic")
    content_extra: dict[str, Any] | None = None
    entity_hints: list[EntityHint] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None

    @field_validator("source")
    @classmethod
    def source_lowercase(cls, v: str) -> str:
        return v.lower().strip()

    @field_validator("occurred_at")
    @classmethod
    def naive_utc(cls, v: datetime) -> datetime:
        """Время со зоной → наивный UTC.

        `events.occurred_at` — `timestamp WITHOUT time zone`, и asyncpg на
        tz-aware значении не приводит его, а падает с DataError. Через
        обработчик FastAPI это выходило пятисоткой на КЛИЕНТСКИЕ данные:
        поймано вживую, когда claude_chat_sync прислал `…Z` из транскриптов
        Claude — 66 файлов, 0 отправлено, и в логе только «HTTP 500».

        Наивный UTC — соглашение всего проекта (см. `parse_date` у trello,
        `parse_ts` у slack), поэтому приводим на приёме: любой будущий клиент
        и вебхук получают его бесплатно, а 500 на входных данных — это баг
        сервера, а не клиента.
        """
        if v.tzinfo is None:
            return v
        return v.astimezone(UTC).replace(tzinfo=None)

    @field_validator("content_text")
    @classmethod
    def normalize_text(cls, v: str) -> str:
        # Убираем NULL-bytes, нормализуем whitespace
        return v.replace("\x00", "").strip()

    @property
    def dedup_key(self) -> str:
        """Ключ для idempotent ingestion — gateway его использует."""
        return f"{self.source}:{self.source_event_id}"

    @property
    def is_outbound(self) -> bool:
        """True если это исходящее событие от Димы (а не ему)."""
        meta = self.metadata or {}
        return bool(meta.get("direction") == "sent" or meta.get("from_me"))
