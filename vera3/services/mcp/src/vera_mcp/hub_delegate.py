"""Агент за MCP-хабом: имя клиента `hub/<агент>` вместо общего `hub`.

Хаб ходит в /mcp одним сервисным токеном (запись `hub` в ROOM_TOKENS и
MCP_TOKENS) и передаёт имя своего агента заголовком X-Vera-Hub-Agent.
Заголовок учитывается ТОЛЬКО у клиентов из HUB_DELEGATE_CLIENTS: у любого
другого токена он ничего не меняет. Префикс `hub/` не даёт выдать себя за
настоящего агента комнаты — ROOM_ATTEST_ACTORS и ROOM_OAUTH_ACTORS его не содержат.
"""
from __future__ import annotations

import os
import re

HUB_AGENT_HEADER = b"x-vera-hub-agent"
_AGENT = re.compile(r"[a-z][a-z0-9_-]{1,31}")


def hub_clients() -> frozenset[str]:
    raw = os.environ.get("HUB_DELEGATE_CLIENTS", "hub")
    return frozenset(n.strip() for n in raw.split(",") if n.strip())


def delegated_client(client: str, headers: list[tuple[bytes, bytes]]) -> str:
    if client not in hub_clients():
        return client
    agent = next((v.decode("latin-1") for k, v in headers if k.lower() == HUB_AGENT_HEADER), "")
    return f"hub/{agent}" if _AGENT.fullmatch(agent) else client
