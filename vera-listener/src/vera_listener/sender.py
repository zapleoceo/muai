"""Отправка готовых сессий в gateway. Нет сети — очередь просто ждёт.

Ответ 4xx (кроме 429) — это не «сеть моргнула», а негодное тело: такой файл
уезжает в failed/, иначе он бесконечно держал бы очередь и заслонял живые
сессии. Всё остальное — ретрай с нарастающей паузой.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from vera_listener.config import Config
from vera_listener.outbox import Outbox, read_payload

log = logging.getLogger("listener.sender")

TIMEOUT_S = 60


class Sender:
    def __init__(self, config: Config, outbox: Outbox):
        self.config = config
        self.outbox = outbox
        self.backoff_s = 0.0

    @property
    def endpoint(self) -> str:
        return f"{self.config.gateway_url}/v1/voice/session"

    def post(self, payload: dict) -> tuple[bool, bool, str]:
        """→ (успех, годное ли тело, пояснение)."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Internal-Secret": self.config.internal_secret},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                return (200 <= response.status < 300, True, f"HTTP {response.status}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:200]
            poison = 400 <= e.code < 500 and e.code not in (408, 429)
            return (False, not poison, f"HTTP {e.code}: {body}")
        except OSError as e:
            return (False, True, f"{type(e).__name__}: {e}")

    def flush(self) -> tuple[int, int]:
        """Отправить всё готовое. → (отправлено, осталось)."""
        sent = 0
        pending = self.outbox.ready()
        for path in pending:
            payload = read_payload(path)
            if payload is None:
                self.outbox.park(path, "файл нечитаем или пуст")
                continue
            ok, retryable, info = self.post(payload)
            if ok:
                self.outbox.drop(path)
                sent += 1
                continue
            if not retryable:
                self.outbox.park(path, info)
                continue
            log.warning("сессия %s не ушла (%s) — остаётся в очереди",
                        path.name, info)
            break

        left = len(self.outbox.ready())
        if sent:
            log.info("отправлено сессий: %d, в очереди: %d", sent, left)
        self.backoff_s = (0.0 if not left else
                          min(max(self.config.send_interval_s, self.backoff_s * 2),
                              self.config.send_backoff_max_s))
        return sent, left
