"""Отправка готовых сессий в gateway. Нет сети — очередь просто ждёт.

Ответ 4xx (кроме 429) — это не «сеть моргнула», а негодное тело: такой файл
уезжает в failed/, иначе он бесконечно держал бы очередь и заслонял живые
сессии. Всё остальное — ретрай с нарастающей паузой; 2xx с `accepted` без
`event_id`/`deduped` тоже (сессия ещё обрабатывается).
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

#: Cloudflare перед шлюзом режет запросы по подписи клиента: с
#: User-Agent «Python-urllib/3.12» он отдаёт 403 error code 1010 («banned
#: based on your browser signature»). Поймано вживую — синк claude не работал с
#: самого начала именно из-за этого, а curl проходил, потому что у него
#: другой UA. Своё имя честнее подделки под браузер и проходит.
USER_AGENT = "vera-listener/1.0 (+https://dima.veranda.my)"


def post_json(url: str, secret: str, payload: dict) -> tuple[bool, bool, str]:
    """→ (успех, годное ли тело, пояснение). Общая для сессий и поручений."""
    ok, retryable, info, _body = post_json_body(url, secret, payload)
    return ok, retryable, info


def still_pending(body: str) -> bool:
    """Шлюз принял сессию, но события ещё нет (`accepted`) — файл держим.

    Не-JSON ответ — старый шлюз: считаем окончательным, как раньше.
    """
    try:
        reply = json.loads(body)
    except ValueError:
        return False
    if not isinstance(reply, dict):
        return False
    return bool(reply.get("accepted")) and not reply.get("event_id")         and not reply.get("deduped")


def post_json_body(url: str, secret: str,
                   payload: dict) -> tuple[bool, bool, str, str]:
    """Как `post_json`, плюс тело успешного ответа."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Internal-Secret": secret,
                 "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = response.read().decode("utf-8", errors="ignore")
            return (200 <= response.status < 300, True, f"HTTP {response.status}", body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:200]
        poison = 400 <= e.code < 500 and e.code not in (408, 429)
        return (False, not poison, f"HTTP {e.code}: {body}", body)
    except OSError as e:
        return (False, True, f"{type(e).__name__}: {e}", "")


class Sender:
    def __init__(self, config: Config, outbox: Outbox):
        self.config = config
        self.outbox = outbox
        self.backoff_s = 0.0

    @property
    def endpoint(self) -> str:
        return f"{self.config.gateway_url}/v1/voice/session"

    def post(self, payload: dict) -> tuple[bool, bool, str]:
        """→ (успех, годное ли тело, пояснение).

        «Принято, свёртка идёт» — не успех: файл остаётся в очереди и уходит
        повторно с обычной паузой, пока шлюз не ответит event_id или deduped.
        Иначе падение фоновой свёртки на сервере теряло бы сессию.
        """
        ok, retryable, info, body = post_json_body(
            self.endpoint, self.config.internal_secret, payload)
        if ok and still_pending(body):
            return (False, True, f"{info} accepted — шлюз ещё обрабатывает")
        return (ok, retryable, info)

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
