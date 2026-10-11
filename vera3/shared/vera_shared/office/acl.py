"""ACL office intake: deny-by-default. Принципал — только из аутентификации, не из тела запроса.

Сервисный принципал видит и меняет лишь собственные задачи своего проекта. Реестр —
OFFICE_SERVICE_TOKENS (`имя:проект:токен,…`), отдельный от MCP_TOKENS и ROOM_TOKENS:
сервисный токен не открывает ни память, ни room_*-инструменты, и наоборот.
"""
from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass

from vera_shared.room.task_fields import PROJECT_RE

MIN_TOKEN_LEN = 32
SERVICE_TYPE = "service"
ACTIONS = frozenset({"create", "answer", "list", "delivered"})
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class OfficeForbidden(PermissionError):
    """Действие или задача вне прав принципала."""


@dataclass(frozen=True)
class OfficePrincipal:
    name: str
    project: str
    type: str = SERVICE_TYPE
    own_tasks: bool = True


def parse_registry(raw: str | None) -> dict[str, OfficePrincipal]:
    """{токен: принципал}. Битая запись или короткий токен — исключение: сервис не стартует."""
    registry: dict[str, OfficePrincipal] = {}
    for item in filter(None, (p.strip() for p in (raw or "").split(","))):
        name, _, rest = item.partition(":")
        project, _, token = rest.partition(":")
        if not _NAME_RE.match(name) or not PROJECT_RE.match(project) or not token:
            raise ValueError("OFFICE_SERVICE_TOKENS entries must look like name:project:token")
        if len(token) < MIN_TOKEN_LEN:
            raise ValueError(f"office token for {name} is shorter than {MIN_TOKEN_LEN} characters")
        if token in registry:
            raise ValueError("OFFICE_SERVICE_TOKENS lists the same token twice")
        registry[token] = OfficePrincipal(name=name, project=project)
    return registry


def check_disjoint(registry: Mapping[str, OfficePrincipal], other_tokens: set[str]) -> None:
    if set(registry) & other_tokens:
        raise ValueError("OFFICE_SERVICE_TOKENS reuse an MCP or room token")


def resolve(provided: str | None, registry: Mapping[str, OfficePrincipal],
            ) -> OfficePrincipal | None:
    """Сравнивает со ВСЕМИ токенами без раннего выхода."""
    if not provided:
        return None
    found: OfficePrincipal | None = None
    for token, principal in registry.items():
        if hmac.compare_digest(provided.encode(), token.encode()):
            found = principal
    return found


def authorize(principal: OfficePrincipal, action: str) -> None:
    if (principal.type != SERVICE_TYPE or not principal.own_tasks
            or action not in ACTIONS):
        raise OfficeForbidden(f"action {action!r} is not allowed")


def require_own(principal: OfficePrincipal, *, creator: str, project: str | None) -> None:
    """Задача чужого создателя или чужого проекта — отказ."""
    if creator != principal.name or project != principal.project:
        raise OfficeForbidden("task does not belong to this principal")
