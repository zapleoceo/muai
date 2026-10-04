"""Synthetic source revisions and claims for shadow model tests."""

from __future__ import annotations

from vera_shared.ingest.shadow_types import Claim, SourceRevision
from vera_shared.db.shadow_repository import ShadowBrain


async def make_brain(path):
    return await ShadowBrain.create(
        path,
        account_scopes={
            ("telegram", "owner-a"): "telegram:owner-a",
            ("telegram", "owner-b"): "telegram:owner-b",
        },
    )


def source(account: str = "owner-a", revision: str = "1") -> SourceRevision:
    return SourceRevision(
        provider="telegram",
        account_id=account,
        object_type="message",
        external_id="chat-7:42",
        revision=revision,
        content_hash=f"hash-{revision}",
        received_at=f"2026-10-03T00:00:0{revision}Z",
        required_scope=f"telegram:{account}",
        previous_revision=str(int(revision) - 1)
        if revision.isdigit() and revision != "1"
        else None,
    )


def decision(value: str = "Ship on Monday", version: str = "extract-1") -> Claim:
    return Claim(
        subject="project:veranda",
        predicate="decision",
        value=value,
        valid_from="2026-10-02T12:00:00Z",
        evidence_anchor="message:0-23",
        evidence_kind="document",
        extraction_version=version,
    )
