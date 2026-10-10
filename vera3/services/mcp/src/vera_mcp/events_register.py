"""Подключение MCP Events к серверу комнаты через публичный add_request_handler."""
from __future__ import annotations

from mcp import types
from mcp.server.mcpserver import MCPServer

from vera_mcp.events_catalog import list_events


def register_events(server: MCPServer) -> None:
    # MCPServer не отдаёт lowlevel-сервер публично; add_request_handler — публичный API Server.
    server._lowlevel_server.add_request_handler("events/list", types.PaginatedRequestParams, list_events)
