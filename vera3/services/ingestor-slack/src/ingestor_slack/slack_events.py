"""Сообщения канала → спеки событий, с необязательными permalink-ссылками."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from vera_shared.db.models_sources import SlackConversationRow

from ingestor_slack.client import SlackClient
from ingestor_slack.mapper import message_to_event
from ingestor_slack.slack_profiles import Profiles

if TYPE_CHECKING:
    from ingestor_slack.poller import PermalinkBudget

log = logging.getLogger("slack")

PERMALINK_TIMEOUT_S = 5
PERMALINK_CONCURRENCY = 8


async def events_from(messages: list[dict], row: SlackConversationRow,
                      me_id: str, account: str, names: Profiles,
                      client: SlackClient, budget: PermalinkBudget) -> list[dict]:
    await names.resolve({str(m.get("user") or "") for m in messages})
    specs = []
    semaphore = asyncio.Semaphore(PERMALINK_CONCURRENCY)

    async def add_permalink(spec: dict, ts: str) -> None:
        async with semaphore:
            if budget.disabled:
                return
            try:
                permalink = await asyncio.wait_for(
                    client.get_permalink(row.conversation_id, ts),
                    timeout=PERMALINK_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 - a link must not block ingestion
                log.warning("slack/%s: permalink unavailable for %s: %s",
                            row.name, ts, e)
                budget.failures += 1
                if budget.failures >= 2:
                    budget.disabled = True
            else:
                if permalink:
                    spec["metadata_"]["permalink"] = permalink

    links = []
    for message in messages:
        spec = message_to_event(
            message, channel_id=row.conversation_id, channel_name=row.name,
            channel_kind=row.kind, is_private=row.is_private,
            me_id=me_id, account=account, names=names.known,
        )
        if spec:
            specs.append(spec)
            links.append((spec, str(message["ts"])))
    if links and not budget.disabled and budget.remaining() > 0:
        tasks = [asyncio.create_task(add_permalink(spec, ts)) for spec, ts in links]
        _, pending = await asyncio.wait(tasks, timeout=budget.remaining())
        if pending:
            budget.disabled = True
            log.warning("slack/%s: permalink lookup budget exhausted; ingesting without links",
                        row.name)
            for task in pending:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return specs
