"""Переразбор старых писем Jira: отбор, идемпотентность, бэкап до записи, откат."""
from __future__ import annotations

import base64
import json
import os

import pytest
from sqlalchemy import select
from vera_shared.db.models import EventRow

os.environ.setdefault("GMAIL_CLIENT_ID", "test-cid")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "test-csec")

from ingestor_gmail import reingest_run, reingest_store  # noqa: E402
from ingestor_gmail.poller import _format_event  # noqa: E402
from ingestor_gmail.reingest import (  # noqa: E402
    CANDIDATE,
    IDENTICAL,
    NO_MARKERS,
    EventSnapshot,
    classify,
    is_jira_event,
    snippet_pair,
)
from ingestor_gmail.reingest_run import RunOptions, run  # noqa: E402

ACC = "me@itstep.org"
JIRA = '"Igor (JIRA)" <jira@itstep.atlassian.net>'
SHOP = "Shop <news@shop.example>"
R = '<span class="diff-removed">'
A = '<span class="diff-added">'
HTML = f"<p>Зачем: {R}платформа платит</span> {A}платформа не платит</span></p>"
PLAIN = "Зачем: платформа платит платформа не платит"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def _msg(mid: str, from_: str, html: str = HTML) -> dict:
    return {"id": mid, "payload": {
        "mimeType": "multipart/alternative",
        "headers": [{"name": "From", "value": from_}, {"name": "To", "value": ACC},
                    {"name": "Subject", "value": "LAM-264"},
                    {"name": "Date", "value": "Mon, 05 Oct 2026 10:00:00 +0000"}],
        "parts": [{"mimeType": "text/plain", "body": {"data": _b64(PLAIN)}},
                  {"mimeType": "text/html", "body": {"data": _b64(html)}}]}}


def _old_text(msg: dict) -> str:
    plain_only = {**msg, "payload": {**msg["payload"], "parts": msg["payload"]["parts"][:1]}}
    return _format_event(ACC, plain_only)["content_text"]


class FakeFetcher:
    def __init__(self, msgs: dict[str, dict]) -> None:
        self.msgs = msgs

    async def get(self, email: str, message_id: str) -> dict | None:
        return self.msgs.get(message_id)


async def _seed(sqlite_db, mid: str, from_: str) -> tuple[int, dict]:
    msg = _msg(mid, from_)
    spec = _format_event(ACC, msg)
    spec["content_text"] = _old_text(msg)
    async with sqlite_db() as s:
        row = EventRow(triage_status="pending", **spec)
        s.add(row)
        await s.flush()
        return row.id, msg


async def _content(sqlite_db, eid: int) -> tuple[str, dict]:
    async with sqlite_db() as s:
        row = (await s.execute(select(EventRow).where(EventRow.id == eid))).scalar_one()
        return row.content_text, row.metadata_


def _opts(tmp_path, **kw) -> RunOptions:
    return RunOptions(reports=tmp_path, **kw)


def test_gate_is_jira_only():
    jira = EventSnapshot(1, "a", ACC, "x", {"from": JIRA})
    shop = EventSnapshot(2, "b", ACC, "x", {"from": SHOP})
    header_only = EventSnapshot(3, "c", ACC, f"Author: {JIRA}\nFrom: {JIRA}\n---\nbody", None)
    assert is_jira_event(jira) and is_jira_event(header_only)
    assert not is_jira_event(shop)


def test_classify_verdicts():
    msg = _msg("m1", JIRA)
    old = EventSnapshot(1, "m1", ACC, _old_text(msg))
    verdict, new = classify(old, msg)
    assert verdict == CANDIDATE and "[удалено: платформа платит]" in new
    assert classify(EventSnapshot(1, "m1", ACC, new), msg)[0] == IDENTICAL
    plain_msg = _msg("m2", JIRA, html="<p>без правок</p>")
    stored = EventSnapshot(2, "m2", ACC, "иной текст")
    assert classify(stored, plain_msg)[0] == NO_MARKERS


def test_snippet_pair_starts_at_first_difference():
    before, after = snippet_pair("aaa bbb ccc", "aaa [x] ccc")
    assert before.startswith("aaa") and "[x]" in after


@pytest.mark.asyncio
async def test_non_jira_untouched_and_idempotent(sqlite_db, tmp_path):
    jira_id, jira_msg = await _seed(sqlite_db, "mj", JIRA)
    shop_id, shop_msg = await _seed(sqlite_db, "ms", SHOP)
    shop_before = (await _content(sqlite_db, shop_id))[0]
    fetcher = FakeFetcher({"mj": jira_msg, "ms": shop_msg})

    dry = await run(_opts(tmp_path), fetcher)
    assert dry["counts"]["jira_events_selected"] == 1 and dry["candidate_ids"] == [jira_id]
    assert dry["llm_calls"] == 0 and dry["applied"] == 0
    assert "[удалено:" not in (await _content(sqlite_db, jira_id))[0]

    first = await run(_opts(tmp_path, apply=True), fetcher)
    assert first["applied"] == 1
    second = await run(_opts(tmp_path, apply=True), fetcher)
    assert second["applied"] == 0 and second["counts"][IDENTICAL] == 1
    assert second["backup_file"] is None
    assert (await _content(sqlite_db, shop_id))[0] == shop_before
    assert "reingested_at" in (await _content(sqlite_db, jira_id))[1]


@pytest.mark.asyncio
async def test_backup_written_before_update(sqlite_db, tmp_path, monkeypatch):
    eid, msg = await _seed(sqlite_db, "mj", JIRA)
    old, _ = await _content(sqlite_db, eid)
    seen: dict = {}
    real_apply = reingest_store.apply_batch

    async def spy(batch):
        backups = list(tmp_path.glob("reingest-jira-backup-*.jsonl"))
        seen["backup"] = [json.loads(x) for p in backups for x in p.read_text("utf-8").splitlines()]
        seen["db_before"] = (await _content(sqlite_db, eid))[0]
        return await real_apply(batch)

    monkeypatch.setattr(reingest_run, "apply_batch", spy)
    await run(_opts(tmp_path, apply=True), FakeFetcher({"mj": msg}))
    assert seen["backup"][0]["id"] == eid and seen["backup"][0]["content_text"] == old
    assert seen["db_before"] == old


@pytest.mark.asyncio
async def test_rollback_restores_old_text_and_metadata(sqlite_db, tmp_path):
    eid, msg = await _seed(sqlite_db, "mj", JIRA)
    old_text, old_meta = await _content(sqlite_db, eid)
    rep = await run(_opts(tmp_path, apply=True), FakeFetcher({"mj": msg}))
    assert (await _content(sqlite_db, eid))[0] != old_text

    restored = await reingest_store.rollback(tmp_path / rep["backup_file"].split("/")[-1])
    text, meta = await _content(sqlite_db, eid)
    assert restored == 1 and text == old_text and meta == old_meta


@pytest.mark.asyncio
async def test_apply_skips_event_changed_after_read(sqlite_db, tmp_path):
    eid, msg = await _seed(sqlite_db, "mj", JIRA)
    counts, cands, _ = await reingest_run.collect(_opts(tmp_path), FakeFetcher({"mj": msg}))
    assert counts[CANDIDATE] == 1
    async with sqlite_db() as s:
        row = (await s.execute(select(EventRow).where(EventRow.id == eid))).scalar_one()
        row.content_text = "правка владельца"
    assert await reingest_store.apply_batch(cands) == 0
    assert (await _content(sqlite_db, eid))[0] == "правка владельца"
