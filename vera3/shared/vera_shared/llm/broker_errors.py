"""Отказы клиента AIbroker и сжатие тела ошибки для сообщения."""
from __future__ import annotations

from vera_shared.redact_view import redact_view


class BrokerCallFailed(Exception):
    """Broker returned non-2xx or all providers exhausted."""


class BrokerJobPending(BrokerCallFailed):
    """The client's poll ceiling passed while the broker still works the job.
    Carries `job_id` so the caller can come back for the result instead of
    resubmitting the same payload."""

    def __init__(self, job_id: int | str, ceiling: float) -> None:
        super().__init__(f"job {job_id} still pending after {ceiling:.0f}s")
        self.job_id = job_id


class BrokerJobGone(BrokerCallFailed):
    """The broker no longer knows this job (404): retention purged it."""

    def __init__(self, job_id: int | str) -> None:
        super().__init__(f"job {job_id} not found on broker")
        self.job_id = job_id


class BrokerJobErrored(BrokerCallFailed):
    """The job reached the terminal `error` state on the broker."""

    def __init__(self, job_id: int | str, error: object) -> None:
        super().__init__(f"job {job_id} failed: {error}")
        self.job_id = job_id


def _short(body: str, limit: int = 200) -> str:
    """Тело ошибки для сообщения: одна строка. HTML-страница Cloudflare
    иначе растекалась многострочником по логам и triage_error."""
    return redact_view(" ".join(body.split())[:limit])
