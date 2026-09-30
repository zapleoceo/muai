"""Очередь голосовых поручений на диске и их отправка на сервер.

Отдельно от очереди сессий по двум причинам. Сессия уходит раз в полминуты
и может подождать; поручение ждёт владелец, поэтому отправщик просыпается
сразу, как только оно легло в очередь. И сессия огромна, а поручение — строка:
заслонять одно другим незачем.

Поручение пишется на диск ДО первой попытки отправки: потеря команды хуже
задержки, а процесс могут закрыть в любую секунду. Сбой сети — файл лежит и
уходит следующей попыткой, в том числе после перезапуска слушателя.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vera_listener.sender import post_json

log = logging.getLogger("listener.command_outbox")

#: Пауза между попытками при сбое сети: быстро вначале — владелец ждёт ответа
#: во время звонка, — и не чаще раза в минуту, если сеть лежит долго.
RETRY_FIRST_S = 2.0
RETRY_MAX_S = 60.0

Post = Callable[[dict[str, Any]], tuple[bool, bool, str]]


class CommandOutbox:
    def __init__(self, queue_dir: Path, post: Post):
        self.ready_dir = queue_dir / "commands"
        self.failed_dir = queue_dir / "commands-failed"
        for path in (self.ready_dir, self.failed_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.post = post
        self.backoff_s = 0.0
        self._wake = threading.Event()

    def put(self, command: dict[str, Any]) -> Path:
        path = self.ready_dir / f"{command['command_id']}.json"
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(command, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        self._wake.set()
        return path

    def ready(self) -> list[Path]:
        return sorted(self.ready_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)

    def flush(self) -> int:
        """Отправить всё, что лежит. → сколько осталось."""
        for path in self.ready():
            try:
                with path.open(encoding="utf-8") as fh:
                    command = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                self._park(path, f"файл нечитаем: {type(e).__name__}")
                continue
            ok, retryable, info = self.post(command)
            if ok:
                path.unlink(missing_ok=True)
                log.info("поручение %s доставлено на сервер", path.stem)
                continue
            if not retryable:
                self._park(path, info)
                continue
            log.warning("поручение %s не ушло (%s) — повторю", path.stem, _kind(info))
            log.debug("поручение %s: ответ сервера %s", path.stem, info)
            break
        left = len(self.ready())
        self.backoff_s = (0.0 if not left else
                          min(max(RETRY_FIRST_S, self.backoff_s * 2), RETRY_MAX_S))
        return left

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
        os.replace(path, self.failed_dir / path.name)


def _kind(info: str) -> str:
    """Только код или тип ошибки. Тело ответа — не для WARNING: pydantic в 422
    цитирует присланное поле, то есть сам текст поручения."""
    return info.split(":", 1)[0]


def gateway_post(gateway_url: str, secret: str) -> Post:
    url = f"{gateway_url}/v1/voice/command"
    return lambda command: post_json(url, secret, command)
