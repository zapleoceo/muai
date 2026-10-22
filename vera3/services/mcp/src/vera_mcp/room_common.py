"""Общие типы аргументов и проверка комнаты для MCP-инструментов комнаты агентов."""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from vera_mcp.auth import allowed_rooms
from vera_mcp.room_oauth import configured_agents

Room = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
Ident = Annotated[str, Field(min_length=1, max_length=128)]
Agent = Annotated[str, Field(min_length=1, max_length=64)]
Consumer = Annotated[str, Field(pattern=r"^[a-z0-9_.-]{1,64}$")]
Project = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
Paths = Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=50)]
MAX_BODY_CHARS = 20_000
DEFAULT_LEASE_S = 900
MAX_LEASE_S = 4 * 3600


def _room(room: str) -> str:
    if room not in allowed_rooms():
        raise ValueError(f"room {room!r} is not open to room tokens")
    return room


def check_session(me: str, session: str | None) -> str | None:
    """Метка сессии не может совпасть с именем другого агента или занять dispatcher: у vera."""
    if session is None or session == me:
        return session
    if session in configured_agents() or (session.startswith("dispatcher:") and me != "vera"):
        raise ValueError("session label is reserved for another agent")
    return session
