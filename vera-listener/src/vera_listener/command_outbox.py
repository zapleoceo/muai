"""Очередь голосовых поручений на диске и их отправка на сервер.

Отдельно от очереди сессий по двум причинам. Сессия уходит раз в полминуты
и может подождать; поручение ждёт владелец, поэтому отправщик просыпается
сразу, как только оно легло в очередь. И сессия огромна, а поручение — строка:
заслонять одно другим незачем.

Поручение пишется на диск ДО первой попытки отправки: потеря команды хуже
задержки, а процесс могут закрыть в любую секунду. Сбой сети — файл лежит и
уходит следующей попыткой, в том числе после перезапуска слушателя.

Пауза у каждого поручения своя: одно со стабильной ошибкой не держит очередь,
остальные уходят мимо него.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vera_listener.sender import post_json

log = logging.getLogger("listener.command_outbox")

#: Пауза между попытками при сбое сети: быстро вначале — владелец ждёт ответа
#: во время звонка, — и не чаще раза в минуту, если сеть лежит долго.
RETRY_FIRST_S = 2.0
RETRY_MAX_S = 60.0

#: «Ядовитые» ответы — само тело негодно, повтор ничего не даст. 401/403/404
#: сюда НЕ входят: шлюз без новой ручки отдаёт 404 (слушатель выкатили раньше
#: сервера), ротация секрета — 401/403. Это поправимо на сервере, и поручение
#: должно дождаться, а не пропасть.
POISON_CODES = frozenset({400, 413, 422})

#: Сколько хранить отложенные поручения. Нужны они для разбора, а не для
#: отправки; неделя покрывает «заметил в понедельник, что было в пятницу».
FAILED_KEEP_S = 7 * 24 * 3600.0

Post = Callable[[dict[str, Any]], tuple[bool, bool, str]]


class CommandOutbox:
    def __init__(self, queue_dir: Path, post: Post, *,
                 clock: Callable[[], float] = time.monotonic):
        self.ready_dir = queue_dir / "commands"
        self.failed_dir = queue_dir / "commands-failed"
        for path in (self.ready_dir, self.failed_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.post = post
        self.clock = clock
        self.backoff_s = 0.0
        #: id поручения → (когда можно снова, текущая пауза).
        self._retry: dict[str, tuple[float, float]] = {}
        self._wake = threading.Event()
        #: id → когда поставлено, для лога «очередь→сервер».
        self._queued: dict[str, float] = {}

    def put(self, command: dict[str, Any]) -> Path:
        path = self.ready_dir / f"{command['command_id']}.json"
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(command, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        self._queued[command["command_id"]] = self.clock()
        self._wake.set()
        return path

    def ready(self) -> list[Path]:
        return sorted(self.ready_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)

    def flush(self) -> int:
        """Отправить всё, чему подошёл срок. → сколько осталось."""
        now = self.clock()
        for path in self.ready():
            due, _ = self._retry.get(path.stem, (0.0, 0.0))
            if due > now:
                continue
            self._send_one(path, now)
        self.prune_failed()
        left = self.ready()
        waits = [self._retry.get(p.stem, (now, 0.0))[0] - now for p in left]
        self.backoff_s = max(0.0, min(waits)) if waits else 0.0
        return len(left)

    def _send_one(self, path: Path, now: float) -> None:
        try:
            with path.open(encoding="utf-8") as fh:
                command = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            self._park(path, f"файл нечитаем: {type(e).__name__}")
            return
        ok, _retryable, info = self.post(command)
        if ok:
            path.unlink(missing_ok=True)
            self._retry.pop(path.stem, None)
            queued = self._queued.pop(path.stem, None)
            log.info("поручение %s доставлено на сервер (очередь→сервер %s)",
                     path.stem, "?" if queued is None
                     else f"{(self.clock() - queued) * 1000:.0f} мс")
            return
        if _code(info) in POISON_CODES:
            self._park(path, info)
            return
        _, delay = self._retry.get(path.stem, (0.0, 0.0))
        delay = min(max(RETRY_FIRST_S, delay * 2), RETRY_MAX_S)
        self._retry[path.stem] = (now + delay, delay)
        log.warning("поручение %s не ушло (%s) — повторю через %.0fс",
                    path.stem, _kind(info), delay)
        log.debug("поручение %s: ответ сервера %s", path.stem, info)

    def prune_failed(self) -> int:
        cutoff = time.time() - FAILED_KEEP_S
        removed = 0
        for path in self.failed_dir.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self._wake.clear()
            try:
                self.flush()
            except Exception as e:                          # noqa: BLE001
                # Поток отправки не имеет права умереть: иначе поручения
                # копились бы на диске молча до перезапуска.
                log.exception("отправка поручений споткнулась: %s", e)
                self.backoff_s = RETRY_MAX_S
            self._wake.wait(self.backoff_s or RETRY_MAX_S)

    def _park(self, path: Path, reason: str) -> None:
        log.warning("поручение %s отложено в commands-failed: %s", path.stem,
                    _kind(reason))
        log.debug("поручение %s: причина %s", path.stem, reason)
        self._retry.pop(path.stem, None)
        os.replace(path, self.failed_dir / path.name)


def _kind(info: str) -> str:
    """Только код или тип ошибки. Тело ответа — не для WARNING: pydantic в 422
    цитирует присланное поле, то есть сам текст поручения."""
    return info.split(":", 1)[0]


def _code(info: str) -> int | None:
    head = _kind(info)
    if head.startswith("HTTP "):
        try:
            return int(head[5:])
        except ValueError:
            return None
    return None


def gateway_post(gateway_url: str, secret: str) -> Post:
    url = f"{gateway_url}/v1/voice/command"
    return lambda command: post_json(url, secret, command)
