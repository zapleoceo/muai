"""Source-linked value types for the offline shadow read model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


class Quarantine(ValueError):
    """The source identity or revision requires manual reconciliation."""


def utc_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Quarantine("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise Quarantine("timestamp needs timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class SourceRevision:
    provider: str
    account_id: str
    object_type: str
    external_id: str
    revision: str
    content_hash: str
    received_at: str
    required_scope: str
    deleted: bool = False
    previous_revision: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.provider, self.account_id, self.object_type, self.external_id


@dataclass(frozen=True)
class Claim:
    subject: str
    predicate: str
    value: str
    valid_from: str
    evidence_anchor: str
    evidence_kind: str
    extraction_version: str
    valid_to: str | None = None


def validate_generation(
    source: SourceRevision,
    claims: list[Claim],
    known_at: str,
) -> tuple[str, str, list[str], list[str | None], str]:
    if not all(
        (
            *source.key,
            source.revision,
            source.content_hash,
            source.received_at,
            source.required_scope,
            known_at,
        )
    ):
        raise Quarantine("incomplete source identity or revision")
    if source.deleted and claims:
        raise Quarantine("tombstone has claims")
    versions = {c.extraction_version for c in claims}
    if len(versions) > 1 or any(
        not all(
            (
                c.subject,
                c.predicate,
                c.value,
                c.valid_from,
                c.evidence_anchor,
                c.evidence_kind,
                c.extraction_version,
            )
        )
        or c.evidence_kind not in {"user_report", "document", "inference"}
        for c in claims
    ):
        raise Quarantine("invalid claim generation")
    valid_from = [utc_timestamp(c.valid_from) for c in claims]
    valid_to = [utc_timestamp(c.valid_to) if c.valid_to else None for c in claims]
    if any(
        end is not None and end < start for start, end in zip(valid_from, valid_to, strict=True)
    ):
        raise Quarantine("valid interval ends before it starts")
    return (
        utc_timestamp(source.received_at),
        utc_timestamp(known_at),
        valid_from,
        valid_to,
        next(iter(versions), "tombstone"),
    )
