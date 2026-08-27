"""Очередь сессий на диске. Переживает падение процесса и отсутствие сети.

Реплики дописываются в open/<id>.jsonl по мере распознавания, а не копятся в
памяти: часовой созвон иначе жил бы одним куском в RAM и терялся целиком при
падении. На закрытии сессии файл атомарно переезжает в ready/, откуда его
забирает отправщик.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("listener.outbox")


class Outbox:
    def __init__(self, queue_dir: Path):
        self.open_dir = queue_dir / "open"
        self.ready_dir = queue_dir / "ready"
        self.failed_dir = queue_dir / "failed"
        for path in (self.open_dir, self.ready_dir, self.failed_dir):
            path.mkdir(parents=True, exist_ok=True)

    def start(self, session_id: str, started_at: str, *, app: str | None,
              window_title: str | None, device_hint: str | None,
              meeting_id: str | None = None, part: int = 1) -> Path:
        path = self.open_dir / f"{session_id}.jsonl"
        self._write_line(path, {
            "kind": "header", "started_at": started_at, "app": app,
            "window_title": window_title, "device_hint": device_hint,
            # Части одной длинной встречи несут общий meeting_id: предохранитель
            # по длительности режет разговор, а связь между половинами должна
            # остаться — иначе в мозге это два независимых события. Первая часть
            # называет встречу собой.
            "meeting_id": meeting_id or session_id, "part": part,
        }, mode="w")
        return path

    def append(self, path: Path, at: float, stream: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._write_line(path, {"kind": "u", "at": round(at, 2),
                                "stream": stream, "text": text})

    def finish(self, path: Path, ended_at: str,
               utterances: list[dict[str, Any]] | None = None) -> Path:
        """Закрыть сессию и перевести в ready/. Список реплик — если чистили эхо."""
        if utterances is not None:
            self._rewrite(path, utterances)
        self._write_line(path, {"kind": "footer", "ended_at": ended_at})
        target = self.ready_dir / path.name
        os.replace(path, target)
        return target

    def _rewrite(self, path: Path, utterances: list[dict[str, Any]]) -> None:
        header = _read_header(path)
        if header is None:
            return
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(header, ensure_ascii=False) + "\n")
            for utt in utterances:
                fh.write(json.dumps({"kind": "u", **utt}, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def ready(self) -> list[Path]:
        return sorted(self.ready_dir.glob("*.jsonl"))

    def drop(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def park(self, path: Path, reason: str) -> None:
        """Ядовитое сообщение — в failed/, чтобы не держало очередь вечно."""
        log.warning("сессия %s отложена в failed: %s", path.name, reason)
        os.replace(path, self.failed_dir / path.name)

    def recover(self, max_age_s: float = 60.0) -> list[Path]:
        """Недописанные сессии после падения — в очередь, а не в мусор.

        Порог был час, и это оставляло мусор навсегда: recover зовётся ТОЛЬКО
        при старте процесса, а при старте ни один файл в open/ не может быть
        живым — писать в него некому. Файлы моложе часа пропускались, второго
        прохода не было, и они копились: 2026-08-27 в open/ лежало пять брошенных
        сессий возрастом от восьми минут. Минута остаётся как защита от гонки с
        только что умершим процессом, который мог не докончить запись строки.
        """
        moved: list[Path] = []
        now = time.time()
        for path in sorted(self.open_dir.glob("*.jsonl")):
            if now - path.stat().st_mtime < max_age_s:
                continue
            payload = read_payload(path)
            if payload is None or not payload.get("utterances"):
                path.unlink(missing_ok=True)
                continue
            target = self.ready_dir / path.name
            os.replace(path, target)
            moved.append(target)
        if moved:
            log.info("подобрано после падения: %d сессий", len(moved))
        return moved

    @staticmethod
    def _write_line(path: Path, obj: dict[str, Any], mode: str = "a") -> None:
        with path.open(mode, encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _read_header(path: Path) -> dict[str, Any] | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("kind") == "header":
            return rec
    return None


def read_payload(path: Path) -> dict[str, Any] | None:
    """Файл очереди → тело запроса для /v1/voice/session."""
    header: dict[str, Any] | None = None
    footer: dict[str, Any] | None = None
    utterances: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Оборванная последняя строка после падения — остальное цело.
                continue
            kind = rec.get("kind")
            if kind == "header":
                header = rec
            elif kind == "footer":
                footer = rec
            elif kind == "u":
                utterances.append({"at": rec.get("at", 0.0),
                                   "stream": rec.get("stream", "mic"),
                                   "text": rec.get("text", "")})
    except OSError as e:
        log.error("не прочитал %s: %s", path, e)
        return None

    if header is None or not utterances:
        return None
    ended_at = (footer or {}).get("ended_at") or _end_from_utterances(
        str(header["started_at"]), utterances)
    return {
        "started_at": header["started_at"],
        "ended_at": ended_at,
        "app": header.get("app"),
        "window_title": header.get("window_title"),
        "device_hint": header.get("device_hint"),
        # Файлы, записанные до появления meeting_id, ещё лежат в очереди —
        # для них часть одна, а встреча совпадает с сессией.
        "meeting_id": header.get("meeting_id") or path.stem,
        "part": int(header.get("part") or 1),
        "utterances": utterances,
    }


def _end_from_utterances(started_at: str, utterances: list[dict[str, Any]]) -> str:
    """Футера нет (падение посреди звонка) — конец берём по последней реплике."""
    last = max((float(u.get("at", 0.0)) for u in utterances), default=0.0)
    try:
        return (datetime.fromisoformat(started_at) + timedelta(seconds=last)).isoformat()
    except ValueError:
        return started_at
