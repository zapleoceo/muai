"""Ошибки апстримов: вид ошибки идёт в журнал, текст — без тел и секретов."""
from __future__ import annotations


class UpstreamError(Exception):
    """`kind`: timeout | busy | down | protocol | rpc. Для rpc — код JSON-RPC апстрима."""

    def __init__(self, kind: str, message: str, code: int = -32000) -> None:
        super().__init__(message[:200])
        self.kind = kind
        self.code = code
