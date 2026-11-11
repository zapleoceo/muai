"""Опрос джобы AIbroker (`/v1/jobs/{id}`) до терминального состояния или дедлайна."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from vera_shared.llm.broker_errors import (
    BrokerCallFailed,
    BrokerJobErrored,
    BrokerJobGone,
    BrokerJobPending,
    _short,
)

# Имя прежнего модуля: опрос жил в broker_client, фильтры журнала завязаны на него.
log = logging.getLogger("vera_shared.llm.broker_client")

#: Ответы на ОПРОС, после которых джоба у брокера, скорее всего, жива: прокси
#: или сам API на мгновение недоступен. 429 — тоже «подожди», не «упало».
_POLL_TRANSIENT = frozenset({429, 500, 502, 503, 504})


async def poll_job(c: httpx.AsyncClient, base_url: str, job_id: int | str, *,
                   ceiling: float, poll_after_s: float) -> dict[str, Any]:
    """Тело ответа джобы в статусе `done`; иначе — исключение.

    Сбой самого ОПРОСА (сеть, 5xx прокси) — не сбой джобы: брокер её считает
    дальше. До 13.09.2026 один 502 от Cloudflare на GET бросал ожидание, id
    джобы терялся, и следующая попытка отправляла фото заново — двойной счёт
    модели. Теперь такой ответ = «ещё не знаю», опрос продолжается до
    дедлайна, а по дедлайну уходит BrokerJobPending с id — как у обычной
    долгой джобы.
    """
    deadline = time.monotonic() + ceiling
    while True:
        await asyncio.sleep(poll_after_s)
        try:
            r = await c.get(f"{base_url}/v1/jobs/{job_id}")
        except Exception as e:  # noqa: BLE001 — сеть мигнула, джоба жива
            log.warning("job %s poll network error (%s) — keep polling", job_id, e)
            r = None
        if r is not None and r.status_code == 404:
            raise BrokerJobGone(job_id)
        if r is None or r.status_code in _POLL_TRANSIENT:
            if r is not None:
                log.warning("job %s poll %s — keep polling", job_id, r.status_code)
            if time.monotonic() > deadline:
                raise BrokerJobPending(job_id, ceiling)
            continue
        if r.status_code >= 400:
            raise BrokerCallFailed(f"broker poll {r.status_code}: {_short(r.text)}")

        data = r.json()
        status = data.get("status")
        if status == "pending":
            if time.monotonic() > deadline:
                raise BrokerJobPending(job_id, ceiling)
            poll_after_s = float(data.get("poll_after_s") or poll_after_s)
            continue
        if status == "error":
            raise BrokerJobErrored(job_id, data.get("error"))
        if status != "done":
            # Malformed/unexpected response (bad JSON, new status we don't
            # know about) must NOT be treated as a silent success with an
            # empty answer — that's worse than a clear error.
            raise BrokerCallFailed(f"job {job_id} unexpected status: {status!r}")
        return data
