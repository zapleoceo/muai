"""Формы запроса и ответа /search."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConversationCtx(BaseModel):
    """Идентификатор разговора — чтобы поиск сам достал историю из БД."""
    chat_id: int
    user_id: int | None = None


class HistoryItem(BaseModel):
    role: str  # "user" | "vera"
    content: str


class SearchQuery(BaseModel):
    q: str = Field(min_length=1)
    limit: int = 15
    days_back: int | None = None
    #: Прямая передача истории (legacy/dashboard)
    history: list[HistoryItem] = Field(default_factory=list)
    #: Правильный путь — бот передаёт chat_id, историю тянет сам поиск
    conversation: ConversationCtx | None = None
    #: ReAct-цикл с вызовом инструментов. По умолчанию включён.
    use_agent: bool = True
    max_steps: int = 6


class SearchResult(BaseModel):
    event_id: int
    source: str
    occurred_at: str
    content_preview: str
    importance: int | None
    score: float


class AnswerResponse(BaseModel):
    answer: str
    results: list[SearchResult]
    provider: str | None
    cost_usd: float
    history_used: int = 0
    agent_steps: int = 0
    agent_trace: list[dict[str, Any]] = Field(default_factory=list)
