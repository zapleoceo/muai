"""Valid and knowledge time behavior in the offline shadow model."""

from __future__ import annotations

from dataclasses import replace

import pytest
from shadow_samples import decision, make_brain, source
from vera_shared.ingest.shadow_types import Quarantine


@pytest.mark.asyncio
async def test_valid_interval_is_returned_with_evidence(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    claim = replace(decision(), valid_to="2026-10-04T12:00:00Z")
    await brain.apply(source(), [claim], known_at="2026-10-03T01:00:00Z")
    current = await brain.current_claims({"telegram:owner-a"})
    assert current[0]["valid_from"] == "2026-10-02T12:00:00.000000+00:00"
    assert current[0]["valid_to"] == "2026-10-04T12:00:00.000000+00:00"
    assert current[0]["evidence_anchor"] == "message:0-23"


@pytest.mark.asyncio
async def test_reversed_valid_interval_is_quarantined(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    claim = replace(decision(), valid_to="2026-10-01T12:00:00Z")
    with pytest.raises(Quarantine, match="valid interval"):
        await brain.apply(
            source(), [claim], known_at="2026-10-03T01:00:00Z", cursor="42"
        )
    assert await brain.checkpoint("telegram", "owner-a") is None


@pytest.mark.asyncio
async def test_invalid_timestamp_and_claim_kind_are_quarantined(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    with pytest.raises(Quarantine, match="invalid timestamp"):
        await brain.apply(source(), [decision()], known_at="yesterday")
    with pytest.raises(Quarantine, match="invalid claim generation"):
        await brain.apply(
            source(),
            [replace(decision(), evidence_kind="command")],
            known_at="2026-10-03T01:00:00Z",
        )


@pytest.mark.asyncio
async def test_tombstone_cannot_carry_claims(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    with pytest.raises(Quarantine, match="tombstone has claims"):
        await brain.apply(
            replace(source(), deleted=True),
            [decision()],
            known_at="2026-10-03T01:00:00Z",
        )


@pytest.mark.asyncio
async def test_out_of_order_revision_rolls_back(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    await brain.apply(
        source(revision="2"), [decision()], known_at="2026-10-03T02:00:00Z", cursor="43"
    )
    with pytest.raises(Quarantine, match="out-of-order"):
        await brain.apply(
            source(revision="1"),
            [decision()],
            known_at="2026-10-03T03:00:00Z",
            cursor="42",
            expected_cursor="43",
        )
    assert await brain.checkpoint("telegram", "owner-a") == "43"


@pytest.mark.asyncio
async def test_equal_receipt_time_cannot_reorder_revisions(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    await brain.apply(source(), [decision()], known_at="2026-10-03T01:00:00Z")
    later = replace(source(revision="2"), received_at=source().received_at)
    with pytest.raises(Quarantine, match="out-of-order"):
        await brain.apply(later, [decision("Changed")], known_at="2026-10-03T02:00:00Z")


@pytest.mark.asyncio
async def test_revision_requires_active_predecessor(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    await brain.apply(source(), [decision()], known_at="2026-10-03T01:00:00Z")
    with pytest.raises(Quarantine, match="out-of-order"):
        await brain.apply(
            replace(source(revision="2"), previous_revision=None),
            [decision("Changed")],
            known_at="2026-10-03T02:00:00Z",
        )


@pytest.mark.asyncio
async def test_source_receipt_provenance_and_knowledge_order(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    await brain.apply(source(), [decision()], known_at="2026-10-03T01:00:00Z")
    with pytest.raises(Quarantine, match="provenance changed"):
        await brain.apply(
            replace(source(), received_at="2026-10-03T00:10:00Z"),
            [decision()],
            known_at="2026-10-03T02:00:00Z",
        )
    with pytest.raises(Quarantine, match="provenance changed"):
        await brain.apply(
            replace(source(), previous_revision="other"),
            [decision()],
            known_at="2026-10-03T02:00:00Z",
        )
    with pytest.raises(Quarantine, match="knowledge precedes"):
        await brain.apply(
            source(revision="2"), [decision()], known_at="2026-10-03T00:00:00Z"
        )


@pytest.mark.asyncio
async def test_new_generation_requires_later_knowledge_time(tmp_path):
    brain = await make_brain(tmp_path / "shadow.db")
    await brain.apply(source(), [decision()], known_at="2026-10-03T01:00:00Z")
    assert not await brain.apply(
        source(), [decision()], known_at="2026-10-03T01:00:00Z"
    )
    with pytest.raises(Quarantine, match="knowledge time must advance"):
        await brain.apply(
            source(revision="2"), [decision()], known_at="2026-10-03T01:00:00Z"
        )
