"""Offline Gmail contract tests against the actual PostgreSQL migrations."""

from __future__ import annotations

import os

import pytest
from shadow_pg_support import isolated_migrated_shadow_schema, use_schema
from sqlalchemy import text
from vera_shared.db.shadow_pg_ingest import PgShadowIngest
from vera_shared.ingest.gmail_pilot import (
    GmailChange,
    apply_next,
    capture_history_page,
    capture_present_resync,
    pilot_status,
    read_pilot,
    record_fetch_result,
    record_history_404,
    register_synthetic_mailbox,
)
from vera_shared.ingest.shadow_types import Quarantine

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 and TEST_DATABASE_URL for isolated PostgreSQL",
)
SUB = "synthetic-gmail-sub"
OTHER = "synthetic-other-sub"
AT = "2026-10-04T00:00:00Z"


def message(
    mid: str, version: str, *, labels: list[str] | None = None
) -> dict[str, object]:
    return {
        "id": mid,
        "historyId": version,
        "text": f"body-{mid}",
        "labels": labels if labels is not None else ["INBOX"],
        "draft": False,
        "attachments": [],
    }


async def setup(conn, schema: str, *, sub: str = SUB, cursor: str = "100"):
    await use_schema(conn, schema)
    await register_synthetic_mailbox(conn, sub, f"{sub}@example.test", cursor)


@pytest.mark.asyncio
async def test_large_noncontiguous_versions_and_same_id_across_subjects(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    huge = 18446744073709551620
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema, cursor=str(huge))
            await register_synthetic_mailbox(
                conn, OTHER, "other@example.test", str(huge)
            )
            for sub, offset in ((SUB, 7), (OTHER, 99)):
                event = GmailChange(str(huge + offset), "same-message", "added")
                assert await capture_history_page(
                    conn,
                    sub,
                    page_id="page-1",
                    start_history_id=str(huge),
                    end_history_id=event.history_id,
                    changes=[event],
                )
                await record_fetch_result(
                    conn,
                    sub,
                    event,
                    verified_sub=sub,
                    outcome="fetched",
                    message=message(event.message_id, str(huge + offset + 11)),
                    observed_at=AT,
                )
                assert await apply_next(conn, sub)
            assert (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain_source_objects "
                        "WHERE provider='gmail-pilot'"
                    )
                )
            ).scalar_one() == 2
            a = await read_pilot(
                conn, SUB, verified_sub=SUB, known_at="2026-10-05T00:00:00Z"
            )
            assert len(a) == 1 and a[0]["account_id"] == SUB
            with pytest.raises(Quarantine, match="subject mismatch"):
                await read_pilot(
                    conn, SUB, verified_sub=OTHER, known_at="2026-10-05T00:00:00Z"
                )


@pytest.mark.asyncio
async def test_duplicate_overlapping_pages_and_fetch_failure_stays_pending(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    first = GmailChange("130", "m1", "added")
    second = GmailChange("170", "m2", "added")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            assert await capture_history_page(
                conn,
                SUB,
                page_id="p1",
                start_history_id="100",
                end_history_id="150",
                changes=[first],
            )
            assert await capture_history_page(
                conn,
                SUB,
                page_id="p2",
                start_history_id="100",
                end_history_id="180",
                changes=[first, second],
            )
            assert not await capture_history_page(
                conn,
                SUB,
                page_id="p1",
                start_history_id="100",
                end_history_id="150",
                changes=[first],
            )
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM brain_gmail_pilot_events")
                )
            ).scalar_one() == 2
            await conn.rollback()
            with pytest.raises(Quarantine, match="changed on replay"):
                await capture_history_page(
                    conn,
                    SUB,
                    page_id="p1",
                    start_history_id="100",
                    end_history_id="150",
                    changes=[],
                )
            await record_fetch_result(
                conn, SUB, first, verified_sub=SUB, outcome="failed"
            )
            assert not await apply_next(conn, SUB)
            status = await pilot_status(conn, SUB)
            assert (
                status["captured_cursor"] == "180" and status["applied_cursor"] == "100"
            )
            await conn.rollback()
            await record_fetch_result(
                conn,
                SUB,
                first,
                verified_sub=SUB,
                outcome="fetched",
                message=message("m1", "200"),
                observed_at=AT,
            )
            assert await apply_next(conn, SUB)
            assert (
                await conn.execute(
                    text(
                        "SELECT fetch_state FROM brain_gmail_pilot_events "
                        "WHERE message_id='m2'"
                    )
                )
            ).scalar_one() == "pending"


@pytest.mark.asyncio
async def test_unseen_old_change_on_overlap_is_rejected(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    deleted = GmailChange("140", "m", "deleted")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="p1",
                start_history_id="100",
                end_history_id="150",
                changes=[deleted],
            )
            with pytest.raises(Quarantine, match="behind captured cursor"):
                await capture_history_page(
                    conn,
                    SUB,
                    page_id="p2",
                    start_history_id="100",
                    end_history_id="180",
                    changes=[
                        GmailChange("130", "m", "added"),
                        deleted,
                        GmailChange("170", "other", "added"),
                    ],
                )
            assert (
                await conn.execute(
                    text("SELECT captured_cursor FROM brain_gmail_pilot_accounts")
                )
            ).scalar_one() == "150"
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM brain_gmail_pilot_events")
                )
            ).scalar_one() == 1


@pytest.mark.asyncio
async def test_unordered_page_applies_add_before_permanent_delete(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    added, deleted = (
        GmailChange("110", "m", "added"),
        GmailChange("140", "m", "deleted"),
    )
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="p",
                start_history_id="100",
                end_history_id="140",
                changes=[deleted, added],
            )
            assert (
                await conn.execute(
                    text("SELECT kind FROM brain_gmail_pilot_events ORDER BY id")
                )
            ).scalars().all() == ["added", "deleted"]
            await conn.rollback()
            await record_fetch_result(
                conn,
                SUB,
                added,
                verified_sub=SUB,
                outcome="fetched",
                message=message("m", "110"),
                observed_at=AT,
            )
            assert await apply_next(conn, SUB)
            assert await apply_next(conn, SUB)
            assert (
                await conn.execute(
                    text("SELECT deleted FROM brain_revisions ORDER BY revision")
                )
            ).scalars().all() == [False, True]


@pytest.mark.asyncio
async def test_empty_history_page_advances_both_cursors(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="empty",
                start_history_id="100",
                end_history_id="190",
                changes=[],
            )
            status = await pilot_status(conn, SUB)
            assert status["captured_cursor"] == status["applied_cursor"] == "190"


@pytest.mark.asyncio
async def test_late_old_fetch_cannot_rewrite_newer_observation(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    old = GmailChange("110", "m", "added")
    later = GmailChange("150", "m", "label_added")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="p",
                start_history_id="100",
                end_history_id="150",
                changes=[old, later],
            )
            await record_fetch_result(
                conn,
                SUB,
                old,
                verified_sub=SUB,
                outcome="fetched",
                message=message("m", "300"),
                observed_at=AT,
            )
            await record_fetch_result(
                conn,
                SUB,
                later,
                verified_sub=SUB,
                outcome="fetched",
                message=message("m", "200"),
                observed_at="2026-10-04T00:00:01Z",
            )
            assert await apply_next(conn, SUB)
            assert await apply_next(conn, SUB)
            assert (
                await conn.execute(
                    text(
                        "SELECT source_head_revision FROM brain_source_objects "
                        "WHERE provider='gmail-pilot'"
                    )
                )
            ).scalar_one() == "300"
            assert (
                await conn.execute(
                    text(
                        "SELECT fetch_state FROM brain_gmail_pilot_events "
                        "WHERE history_id='150'"
                    )
                )
            ).scalar_one() == "obsolete"
            assert (
                await conn.execute(text("SELECT count(*) FROM brain_generations"))
            ).scalar_one() == 1


@pytest.mark.asyncio
async def test_expired_history_break_and_present_state_resync(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="before-gap",
                start_history_id="100",
                end_history_id="120",
                changes=[GmailChange("110", "unfetched", "added")],
            )
            await record_history_404(conn, SUB)
            with pytest.raises(Quarantine, match="coverage gap"):
                await capture_history_page(
                    conn,
                    SUB,
                    page_id="stale",
                    start_history_id="120",
                    end_history_id="200",
                    changes=[],
                )
            await capture_present_resync(conn, SUB, "500", [message("m", "490")], AT)
            status = await pilot_status(conn, SUB)
            assert (
                status["coverage_break"] is True and status["resync_required"] is False
            )
            assert status["captured_cursor"] == "500"
            assert status["counts"].get("gap") == 1
            await conn.rollback()
            assert await apply_next(conn, SUB)
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM brain_gmail_pilot_events")
                )
            ).scalar_one() == 2
            assert (
                await conn.execute(text("SELECT count(*) FROM brain_gmail_pilot_pages"))
            ).scalar_one() == 1


@pytest.mark.asyncio
async def test_empty_present_resync_keeps_coverage_break_and_advances_cursors(
    monkeypatch,
):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            original = GmailChange("110", "previously-live", "added")
            await capture_history_page(
                conn,
                SUB,
                page_id="before",
                start_history_id="100",
                end_history_id="110",
                changes=[original],
            )
            await record_fetch_result(
                conn,
                SUB,
                original,
                verified_sub=SUB,
                outcome="fetched",
                message=message("previously-live", "110"),
                observed_at=AT,
            )
            assert await apply_next(conn, SUB)
            await record_history_404(conn, SUB)
            await capture_present_resync(conn, SUB, "500", [], "2026-10-04T01:00:00Z")
            status = await pilot_status(conn, SUB)
            assert status["coverage_break"] is True
            assert status["captured_cursor"] == status["applied_cursor"] == "500"
            assert (
                len(
                    await read_pilot(
                        conn, SUB, verified_sub=SUB, known_at="2026-10-04T00:30:00Z"
                    )
                )
                == 1
            )
            assert (
                await read_pilot(
                    conn, SUB, verified_sub=SUB, known_at="2026-10-04T02:00:00Z"
                )
                == []
            )
            await conn.rollback()
            assert not await apply_next(conn, SUB)


@pytest.mark.asyncio
async def test_resync_hides_old_body_until_new_observation_is_applied(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    old = GmailChange("110", "m", "added")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="old",
                start_history_id="100",
                end_history_id="110",
                changes=[old],
            )
            await record_fetch_result(
                conn,
                SUB,
                old,
                verified_sub=SUB,
                outcome="fetched",
                message=message("m", "110"),
                observed_at=AT,
            )
            assert await apply_next(conn, SUB)
            await record_history_404(conn, SUB)
            updated = message("m", "490")
            updated["text"] = "new body"
            await capture_present_resync(
                conn, SUB, "500", [updated], "2026-10-04T01:00:00Z"
            )
            assert (
                await read_pilot(
                    conn, SUB, verified_sub=SUB, known_at="2026-10-04T01:30:00Z"
                )
                == []
            )
            await conn.rollback()
            assert await apply_next(conn, SUB)
            claims = await read_pilot(
                conn, SUB, verified_sub=SUB, known_at="2026-10-04T01:30:00Z"
            )
            assert [item["value"] for item in claims] == ["new body"]


@pytest.mark.asyncio
async def test_repeated_resync_requires_monotonic_observation_time(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await record_history_404(conn, SUB)
            await capture_present_resync(conn, SUB, "500", [], "2026-10-04T01:00:00Z")
            await record_history_404(conn, SUB)
            for timestamp in ("2026-10-04T00:30:00Z", "2026-10-04T01:00:00Z"):
                with pytest.raises(Quarantine, match="observation time must advance"):
                    await capture_present_resync(conn, SUB, "600", [], timestamp)
            await capture_present_resync(conn, SUB, "600", [], "2026-10-04T02:00:00Z")
            assert (await pilot_status(conn, SUB))["captured_cursor"] == "600"


@pytest.mark.asyncio
async def test_deletion_before_fetch_and_trash_is_not_permanent_delete(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    added = GmailChange("110", "gone", "added")
    deleted = GmailChange("140", "gone", "deleted")
    trash = GmailChange("160", "retained", "label_added")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="p",
                start_history_id="100",
                end_history_id="170",
                changes=[added, deleted, trash],
            )
            await record_fetch_result(
                conn, SUB, added, verified_sub=SUB, outcome="deleted_before_fetch"
            )
            assert await apply_next(conn, SUB)
            assert await apply_next(conn, SUB)
            await record_fetch_result(
                conn,
                SUB,
                trash,
                verified_sub=SUB,
                outcome="fetched",
                message=message("retained", "180", labels=["TRASH"]),
                observed_at=AT,
            )
            assert await apply_next(conn, SUB)
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM brain_revisions WHERE deleted=true")
                )
            ).scalar_one() == 0
            assert (
                await conn.execute(text("SELECT count(*) FROM brain_claims"))
            ).scalar_one() == 1
            assert (await pilot_status(conn, SUB))["coverage_break"] is True


@pytest.mark.asyncio
async def test_revoke_blocks_capture_apply_and_read(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    event = GmailChange("110", "m", "added")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="p",
                start_history_id="100",
                end_history_id="110",
                changes=[event],
            )
            await record_fetch_result(
                conn,
                SUB,
                event,
                verified_sub=SUB,
                outcome="fetched",
                message=message("m", "120"),
                observed_at=AT,
            )
            async with conn.begin():
                await conn.execute(
                    text("UPDATE brain_gmail_pilot_accounts SET is_active=false")
                )
            with pytest.raises(Quarantine, match="revoked"):
                await apply_next(conn, SUB)
            with pytest.raises(Quarantine, match="revoked"):
                await capture_history_page(
                    conn,
                    SUB,
                    page_id="p2",
                    start_history_id="110",
                    end_history_id="130",
                    changes=[],
                )
            with pytest.raises(Quarantine, match="revoked"):
                await read_pilot(
                    conn, SUB, verified_sub=SUB, known_at="2026-10-05T00:00:00Z"
                )


@pytest.mark.asyncio
async def test_missing_forbidden_and_subject_mismatch_are_distinct(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    first, second = GmailChange("110", "m1", "added"), GmailChange("120", "m2", "added")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="p",
                start_history_id="100",
                end_history_id="120",
                changes=[first, second],
            )
            with pytest.raises(Quarantine, match="subject mismatch"):
                await record_fetch_result(
                    conn, SUB, first, verified_sub=OTHER, outcome="missing"
                )
            await record_fetch_result(
                conn, SUB, first, verified_sub=SUB, outcome="missing"
            )
            await record_fetch_result(
                conn, SUB, second, verified_sub=SUB, outcome="forbidden"
            )
            assert not await apply_next(conn, SUB)
            assert (
                await conn.execute(
                    text("SELECT fetch_state FROM brain_gmail_pilot_events ORDER BY id")
                )
            ).scalars().all() == ["missing", "forbidden"]


@pytest.mark.asyncio
async def test_invalid_observation_quarantines_without_changing_head(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    first, second = (
        GmailChange("110", "m", "added"),
        GmailChange("150", "m", "label_added"),
    )
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="p",
                start_history_id="100",
                end_history_id="150",
                changes=[first, second],
            )
            await record_fetch_result(
                conn,
                SUB,
                first,
                verified_sub=SUB,
                outcome="fetched",
                message=message("m", "200"),
                observed_at=AT,
            )
            await record_fetch_result(
                conn,
                SUB,
                second,
                verified_sub=SUB,
                outcome="fetched",
                message=message("m", "300"),
                observed_at="2026-10-03T00:00:00Z",
            )
            assert await apply_next(conn, SUB)
            assert await apply_next(conn, SUB)
            assert not await apply_next(conn, SUB)
            assert (
                await conn.execute(
                    text(
                        "SELECT source_head_revision FROM brain_source_objects "
                        "WHERE provider='gmail-pilot'"
                    )
                )
            ).scalar_one() == "200"
            assert (
                await conn.execute(
                    text(
                        "SELECT fetch_state FROM brain_gmail_pilot_events "
                        "WHERE history_id='150'"
                    )
                )
            ).scalar_one() == "quarantined"


@pytest.mark.asyncio
async def test_apply_crash_before_ack_retries_same_observation(monkeypatch):
    monkeypatch.setenv("VERA_SHADOW_GMAIL_PILOT_ENABLED", "1")
    change = GmailChange("110", "m", "added")
    async with isolated_migrated_shadow_schema() as (engine, schema):  # noqa: SIM117
        async with engine.connect() as conn:
            await setup(conn, schema)
            await capture_history_page(
                conn,
                SUB,
                page_id="p",
                start_history_id="100",
                end_history_id="110",
                changes=[change],
            )
            await record_fetch_result(
                conn,
                SUB,
                change,
                verified_sub=SUB,
                outcome="fetched",
                message=message("m", "110"),
                observed_at=AT,
            )
            original = PgShadowIngest.apply

            async def crash_after_apply(self, *args, **kwargs):
                await original(self, *args, **kwargs)
                monkeypatch.setattr(PgShadowIngest, "apply", original)
                raise RuntimeError("worker stopped before acknowledgement")

            monkeypatch.setattr(PgShadowIngest, "apply", crash_after_apply)
            with pytest.raises(RuntimeError, match="before acknowledgement"):
                await apply_next(conn, SUB)
            assert (
                await conn.execute(text("SELECT count(*) FROM brain_generations"))
            ).scalar_one() == 0
            await conn.rollback()
            assert await apply_next(conn, SUB)
            assert not await apply_next(conn, SUB)
            assert (
                await conn.execute(text("SELECT count(*) FROM brain_generations"))
            ).scalar_one() == 1
