"""Оркестрация переразбора: Gmail → classify → отчёт; по --apply — бэкап и запись."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from vera_shared.crypto import decrypt
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import GmailAccountRow
from vera_shared.text_chunks import CHUNK_THRESHOLD, LLM_EXCERPT_CHARS
from vera_shared.timeutil import utc_naive_now

from ingestor_gmail.poller import fetch_full_messages, refresh_access
from ingestor_gmail.reingest import (
    CANDIDATE,
    IDENTICAL,
    NO_MARKERS,
    Candidate,
    classify,
    snippet_pair,
)
from ingestor_gmail.reingest_store import (
    apply_batch,
    embedded_ids,
    load_jira_events,
    write_backup,
)

log = logging.getLogger("reingest-jira")
CHARS_PER_TOKEN = 3.0
SNIPPETS_IN_REPORT = 30


@dataclass
class RunOptions:
    apply: bool = False
    event_ids: list[int] | None = None
    since: datetime | None = None
    limit: int | None = None
    batch: int = 50
    reports: Path = Path("/reports")


class MessageFetcher:
    """Токен доступа на аккаунт — один refresh на прогон, не на письмо."""

    def __init__(self) -> None:
        self._tokens: dict[str, str | None] = {}

    async def _token(self, email: str) -> str | None:
        if email not in self._tokens:
            async with get_session() as s:
                acc = (await s.execute(select(GmailAccountRow).where(
                    GmailAccountRow.email == email))).scalar_one_or_none()
            self._tokens[email] = None
            if acc is not None and not acc.needs_reauth:
                tok = await refresh_access(decrypt(acc.refresh_token_enc))
                self._tokens[email] = tok["access_token"]
        return self._tokens[email]

    async def get(self, email: str, message_id: str) -> dict | None:
        token = await self._token(email)
        if token is None:
            return None
        msgs = await fetch_full_messages(token, [message_id])
        return msgs[0] if msgs else None


def _embed_estimate(cands: list[Candidate], have: set[int]) -> dict[str, int]:
    with_vec = [c for c in cands if c.event.id in have]
    chars = sum(min(len(c.new_text), LLM_EXCERPT_CHARS) for c in with_vec)
    return {"candidates_with_vector": len(with_vec),
            "candidates_over_chunk_threshold":
                sum(1 for c in cands if len(c.new_text) > CHUNK_THRESHOLD),
            "reembed_tokens_event_vectors": int(chars / CHARS_PER_TOKEN)}


async def collect(opts: RunOptions, fetcher: MessageFetcher) -> tuple[dict, list[Candidate], list[float]]:
    events = await load_jira_events(opts.event_ids, opts.since, opts.limit)
    counts = {"jira_events_selected": len(events), CANDIDATE: 0, IDENTICAL: 0,
              NO_MARKERS: 0, "fetch_failed": 0}
    cands: list[Candidate] = []
    timings: list[float] = []
    for ev in events:
        t0 = time.perf_counter()
        msg = await fetcher.get(ev.account or "", ev.source_event_id)
        if msg is None:
            counts["fetch_failed"] += 1
            continue
        verdict, new = classify(ev, msg)
        ms = (time.perf_counter() - t0) * 1000
        timings.append(ms)
        counts[verdict] += 1
        if verdict == CANDIDATE:
            cands.append(Candidate(ev, new, ms))
    return counts, cands, timings


async def run(opts: RunOptions, fetcher: MessageFetcher | None = None) -> dict:
    counts, cands, timings = await collect(opts, fetcher or MessageFetcher())
    applied = 0
    stamp = utc_naive_now().strftime("%Y%m%dT%H%M%S")
    backup = opts.reports / f"reingest-jira-backup-{stamp}.jsonl"
    opts.reports.mkdir(parents=True, exist_ok=True)
    if opts.apply:
        for i in range(0, len(cands), opts.batch):
            batch = cands[i:i + opts.batch]
            write_backup(backup, batch)
            applied += await apply_batch(batch)

    report = {
        "mode": "apply" if opts.apply else "dry-run", "at": stamp, "counts": counts,
        "applied": applied, "backup_file": str(backup) if opts.apply and cands else None,
        "llm_calls": 0,
        "timing_ms_per_message": {
            "avg": round(sum(timings) / len(timings), 1) if timings else 0,
            "max": round(max(timings), 1) if timings else 0},
        "embeddings": _embed_estimate(cands, await embedded_ids([c.event.id for c in cands])),
        "candidates": [{
            "event_id": c.event.id, "source_event_id": c.event.source_event_id,
            "old_len": len(c.event.content_text), "new_len": len(c.new_text),
            "ms": round(c.ms, 1),
            "before_after": dict(zip(("before", "after"),
                                     snippet_pair(c.event.content_text, c.new_text),
                                     strict=True)),
        } for c in cands[:SNIPPETS_IN_REPORT]],
        "candidate_ids": [c.event.id for c in cands],
    }
    out = opts.reports / f"reingest-jira-{report['mode']}-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_file"] = str(out)
    return report
