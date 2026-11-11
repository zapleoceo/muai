"""Формы запроса/ответа `/v1/claude/session`."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str

    @field_validator("text")
    @classmethod
    def drop_nul(cls, v: str) -> str:
        # Postgres не хранит \u0000 ни в text, ни в jsonb: один NUL из вывода
        # ssh-приглашения («Enter the user name…: \0») отдавал 500 на каждый
        # проход, и сессия не попадала в мозг никогда (2026-09-25).
        return v.replace("\x00", "")


class ClaudeSession(BaseModel):
    session_id: str = Field(max_length=64)
    project_dir: str = Field(max_length=255)
    started_at: datetime
    ended_at: datetime
    cwd: str | None = None
    git_branch: str | None = Field(default=None, max_length=255)
    turns: list[Turn] = Field(min_length=1)
    # Сколько реплик в сессии на самом деле. Длинную сессию клиент прореживает,
    # чтобы влезть в лимит тела nginx, но курсор обязан считать ВСЕ реплики:
    # иначе дописанная сессия, ужатая до того же размера, не вернётся в очередь.
    turn_count: int | None = Field(default=None, ge=1)


class ClaudeSessionAccepted(BaseModel):
    ok: bool
    status: str
    turns: int
    # Реплик не прибавилось — сессия уже осмыслена в этом объёме.
    unchanged: bool = False


class ClaudeSessionStatus(BaseModel):
    status: str
    turns: int
    done_turns: int
    event_id: int | None = None
    attempts: int = 0
    error: str | None = None
