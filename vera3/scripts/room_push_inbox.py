"""Инбокс слушателя: JSONL на диске + блокирующее ожидание новых сообщений без опроса.

`wait_after` спит на условной переменной, пока не придёт сообщение с номером больше
`after`: фоновая команда `room_push_listener.py wait` завершается ровно в момент прихода,
и сессия Claude просыпается на её выходе, не тратя токены на опрос.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

KEEP_IN_MEMORY = 500


class Inbox:
    def __init__(self, path: Path, keep: int = KEEP_IN_MEMORY) -> None:
        self._path = path
        self._keep = keep
        self._cond = threading.Condition()
        self._records: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self._seq = 0
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._remember(json.loads(line))

    @property
    def latest(self) -> int:
        with self._cond:
            return self._seq

    def _remember(self, record: dict[str, Any]) -> None:
        self._seq += 1
        record["seq"] = self._seq
        self._ids.add(record["event_id"])
        self._records.append(record)
        del self._records[:-self._keep]

    def add(self, event_id: str, payload: dict[str, Any]) -> bool:
        """False — повтор того же webhook-id: сервер переслал, второй записи нет."""
        with self._cond:
            if event_id in self._ids:
                return False
            record = {"event_id": event_id, "received_at": time.time(), "payload": payload}
            self._remember(record)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._cond.notify_all()
            return True

    def wait_after(self, after: int, timeout: float) -> list[dict[str, Any]]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq > after, timeout=timeout)
            return [r for r in self._records if r["seq"] > after]
