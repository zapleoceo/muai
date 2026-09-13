"""Длинный текст: приём без обрезки на 8000, отрывок для LLM, куски для векторов.

До 2026-09-13 gmail/voice/claude_chat резали текст до 8000 символов на входе
(за 30 дней 163 письма и 95 сессий у потолка) — хвост терялся навсегда, а
длинное событие представлял один размытый вектор.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from vera_shared import text_chunks as tc
from vera_shared.db import chunk_vectors

# ─── нарезка ────────────────────────────────────────────────────────────────


def test_empty_and_short():
    assert tc.split_chunks("") == []
    assert tc.split_chunks(None) == []
    assert tc.split_chunks("   \n ") == []
    assert tc.split_chunks("коротко") == ["коротко"]


def test_chunks_respect_size_and_cover_the_whole_text():
    words = [f"слово{i}" for i in range(2000)]
    text = " ".join(words)
    chunks = tc.split_chunks(text, size=500, overlap=80, max_chunks=1000)
    assert all(len(c) <= 500 for c in chunks)
    joined = " ".join(chunks)
    assert all(w in joined for w in words)
    assert chunks[-1].endswith("слово1999")


def test_neighbours_overlap_and_start_on_a_word():
    text = " ".join(f"w{i:04d}" for i in range(600))
    chunks = tc.split_chunks(text, size=300, overlap=60, max_chunks=100)
    for a, b in zip(chunks, chunks[1:], strict=False):
        first = b.split(" ")[0]
        assert first in a.split(" "), "у соседей нет общего слова"
        assert first.startswith("w") and len(first) == 5, "кусок начат с середины слова"


def test_cut_prefers_paragraph_then_sentence():
    para = "А" * 700 + "\n\n" + "Б" * 700
    assert tc.split_chunks(para, size=1000, overlap=100)[0] == "А" * 700
    sent = "Первое предложение. " * 40
    first = tc.split_chunks(sent, size=500, overlap=50)[0]
    assert first.endswith("предложение.")


def test_unicode_is_never_split_inside_a_character():
    text = "😀кот🐈 " * 1000
    chunks = tc.split_chunks(text, size=333, overlap=40, max_chunks=100)
    assert chunks and all(isinstance(c, str) for c in chunks)
    assert all(c.encode("utf-8").decode("utf-8") == c for c in chunks)
    assert all(set(c) <= set("😀кот🐈 ") for c in chunks)


def test_text_without_spaces_is_cut_by_window_and_progresses():
    chunks = tc.split_chunks("x" * 5000, size=1000, overlap=100, max_chunks=100)
    assert len(chunks) == 6 and all(len(c) <= 1000 for c in chunks)


def test_chunk_limit_per_event():
    assert len(tc.split_chunks("слово " * 50_000)) == tc.MAX_CHUNKS


def test_overlap_must_be_smaller_than_size():
    with pytest.raises(ValueError):
        tc.split_chunks("abc", size=10, overlap=10)


def test_limits_cover_the_content_ceiling():
    step = tc.CHUNK_CHARS - tc.CHUNK_OVERLAP
    assert tc.MAX_CHUNKS * step >= tc.MAX_CONTENT_CHARS * 0.95
    assert tc.CHUNK_THRESHOLD < tc.LLM_EXCERPT_CHARS < tc.MAX_CONTENT_CHARS


def test_needs_chunks_threshold():
    assert not tc.needs_chunks(None)
    assert not tc.needs_chunks("x" * tc.CHUNK_THRESHOLD)
    assert tc.needs_chunks("x" * (tc.CHUNK_THRESHOLD + 1))


def test_llm_excerpt_keeps_head_and_tail_within_limit():
    short = "x" * 100
    assert tc.llm_excerpt(short) == short
    text = "H" * 20_000 + "ПОДПИСЬ"
    out = tc.llm_excerpt(text)
    assert len(out) == tc.LLM_EXCERPT_CHARS
    assert out.startswith("HHH") and out.endswith("ПОДПИСЬ") and "…" in out


def test_clip_content():
    assert len(tc.clip_content("y" * 50_000)) == tc.MAX_CONTENT_CHARS
    assert tc.clip_content("abc") == "abc"


# ─── миграция 032 = хелпер ──────────────────────────────────────────────────


def test_migration_032_matches_the_schema_helper():
    """Индекс кусков строится выражением хелпера — запрос поиска повторяет его
    же. Разойдётся миграция с кодом — поиск по кускам уйдёт в seq scan."""
    sql = (Path(__file__).resolve().parents[2] / "infra" / "migrations"
           / "032_event_chunk_embeddings.sql").read_text(encoding="utf-8")
    for stmt in chunk_vectors.chunk_schema_sql(1024):
        assert stmt in sql, stmt
    assert "CONCURRENTLY" not in chunk_vectors.chunk_schema_sql(1024)[2]


# ─── приём без обрезки ──────────────────────────────────────────────────────


def test_gmail_keeps_text_past_8000(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "test-cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "test-csec")
    from ingestor_gmail.poller import _format_event

    body = "начало письма " + "текст " * 3000 + " ХВОСТ-ДОГОВОРЁННОСТЬ"
    msg = {"id": "m1", "payload": {
        "mimeType": "text/plain",
        "headers": [{"name": "From", "value": "a@x"}, {"name": "Subject", "value": "s"}],
        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}
    ev = _format_event("me@x", msg)
    assert len(body) > 8000
    assert "ХВОСТ-ДОГОВОРЁННОСТЬ" in ev["content_text"]


class _Sess:
    def __init__(self):
        self.params: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        self.params = stmt.compile().params

        class R:
            def scalar_one_or_none(self):
                return 1

        return R()


_LONG = {"summary": "Итог.", "outline": [f"шаг {i} " + "подробно " * 20 for i in range(80)]
         + ["ПОСЛЕДНИЙ-ШАГ"]}


@pytest.mark.asyncio
async def test_claude_session_keeps_summary_past_8000():
    import gateway.claude_session as cs

    class _Row:
        session_id, project_dir, cwd, git_branch = "s1", "p", None, "master"
        started_at = ended_at = datetime(2026, 9, 1)
        turn_count = 3

    sess = _Sess()
    with patch("gateway.claude_session.get_session", lambda: sess):
        await cs.store_summary(_Row(), _LONG, {})
    body = sess.params["content_text"]
    assert len(body) > 8000 and "ПОСЛЕДНИЙ-ШАГ" in body


@pytest.mark.asyncio
async def test_voice_keeps_summary_past_8000():
    import gateway.voice as v
    from gateway.voice import Utterance, VoiceSession
    from vera_shared.llm import fold as fold_mod

    t0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    body = VoiceSession(started_at=t0, ended_at=t0 + timedelta(minutes=5),
                        app="zoom", window_title="w",
                        utterances=[Utterance(at=0.0, stream="mic", text="привет")])
    sess = _Sess()
    with patch.object(fold_mod, "chat_async",
                      AsyncMock(return_value=(json.dumps(_LONG), {}))), \
         patch("gateway.voice.get_session", lambda: sess), \
         patch("gateway.voice.check_internal_secret", lambda s: None):
        await v.ingest_voice_session(body, x_internal_secret="ok")
    text = sess.params["content_text"]
    assert len(text) > 8000 and "ПОСЛЕДНИЙ-ШАГ" in text


# ─── триаж пишет куски только длинным ───────────────────────────────────────


@pytest.mark.asyncio
async def test_triage_chunks_only_long_events(monkeypatch):
    from brain_triage import chunks

    short, long_ = "коротко", "абзац. " * 2000
    embed = AsyncMock(side_effect=lambda texts: [[0.1, 0.2]] * len(texts))
    replace, drop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(chunks, "chunk_table_available", AsyncMock(return_value=True))
    monkeypatch.setattr(chunks, "_embed_batch", embed)
    monkeypatch.setattr(chunks, "replace_event_chunks", replace)
    monkeypatch.setattr(chunks, "drop_event_chunks", drop)

    assert await chunks.embed_event_chunks([(1, short), (2, long_)]) == 1
    expected = len(tc.split_chunks(long_))
    assert replace.await_args.args[0] == 2
    assert len(replace.await_args.args[1]) == expected
    drop.assert_awaited_once_with([1])
    sent = [t for call in embed.await_args_list for t in call.args[0]]
    assert len(sent) == expected and short not in sent
    assert all(len(call.args[0]) <= chunks.EMBED_BATCH for call in embed.await_args_list)


@pytest.mark.asyncio
async def test_triage_skips_chunks_before_migration(monkeypatch):
    from brain_triage import chunks

    embed = AsyncMock()
    monkeypatch.setattr(chunks, "chunk_table_available", AsyncMock(return_value=False))
    monkeypatch.setattr(chunks, "_embed_batch", embed)
    assert await chunks.embed_event_chunks([(2, "x" * 9000)]) == 0
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_chunk_embedding_keeps_event_without_chunks(monkeypatch):
    from brain_triage import chunks

    replace = AsyncMock()
    monkeypatch.setattr(chunks, "chunk_table_available", AsyncMock(return_value=True))
    monkeypatch.setattr(chunks, "_embed_batch",
                        AsyncMock(side_effect=lambda t: [None] * len(t)))
    monkeypatch.setattr(chunks, "replace_event_chunks", replace)
    monkeypatch.setattr(chunks, "drop_event_chunks", AsyncMock())
    assert await chunks.embed_event_chunks([(3, "слово " * 2000)]) == 0
    replace.assert_not_awaited()


@pytest.mark.asyncio
async def test_chunk_store_failure_does_not_stop_other_events(monkeypatch):
    from brain_triage import chunks

    monkeypatch.setattr(chunks, "chunk_table_available", AsyncMock(return_value=True))
    monkeypatch.setattr(chunks, "_embed_batch",
                        AsyncMock(side_effect=lambda t: [[1.0]] * len(t)))
    monkeypatch.setattr(chunks, "replace_event_chunks",
                        AsyncMock(side_effect=[RuntimeError("битое"), None]))
    monkeypatch.setattr(chunks, "drop_event_chunks",
                        AsyncMock(side_effect=RuntimeError("таблицу снесли")))
    long_ = "слово " * 2000
    assert await chunks.embed_event_chunks([(4, long_), (5, long_)]) == 1


@pytest.mark.asyncio
async def test_chunk_capability_is_false_on_sqlite(sqlite_db):
    assert await chunk_vectors.chunk_table_available() is False
    assert await chunk_vectors.chunk_ann_available() is False


# ─── скрипт для существующих длинных событий ────────────────────────────────


def _backfill_chunks():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "backfill_chunks",
        Path(__file__).resolve().parents[2] / "scripts" / "backfill_chunks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_estimate_counts_chunks_and_tokens():
    mod = _backfill_chunks()
    text = "слово " * 1500
    got = mod.estimate([(1, text), (2, text)])
    per = len(tc.split_chunks(text))
    assert got["events"] == 2 and got["chunks"] == 2 * per
    assert got["tokens"] == int(got["chars"] / mod.CHARS_PER_TOKEN)


@pytest.mark.asyncio
async def test_backfill_refuses_without_migration(monkeypatch):
    mod = _backfill_chunks()
    monkeypatch.setattr(mod, "init_engine", AsyncMock())
    monkeypatch.setattr(mod, "chunk_table_available", AsyncMock(return_value=False))
    with pytest.raises(SystemExit):
        await mod.run(10, 10, True)


@pytest.mark.asyncio
async def test_backfill_walks_by_id_and_embeds(monkeypatch):
    mod = _backfill_chunks()
    pages = [[(1, "a" * 5000), (2, "b" * 5000)], [(3, "c" * 5000)], []]
    fetch = AsyncMock(side_effect=pages)
    embed = AsyncMock(return_value=1)
    monkeypatch.setattr(mod, "init_engine", AsyncMock())
    monkeypatch.setattr(mod, "chunk_table_available", AsyncMock(return_value=True))
    monkeypatch.setattr(mod, "fetch_pending", fetch)
    monkeypatch.setattr(mod, "embed_event_chunks", embed)
    assert await mod.run(2, 100, False) == {"events": 3}
    assert [c.args[0] for c in fetch.await_args_list] == [0, 2, 3]
    assert embed.await_count == 2
