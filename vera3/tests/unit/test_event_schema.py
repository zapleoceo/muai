"""Тесты RawEvent — каноническая schema."""
from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from vera_shared.events.schema import (
    EntityHint,
    RawEvent,
    Signal,
    TriageMetadata,
)


class TestRawEventBasic:
    def test_minimal_event_valid(self):
        e = RawEvent(
            source="gmail",
            source_event_id="msg_123",
            occurred_at=datetime(2026, 6, 8, 14, 30),
            content_text="Hello",
        )
        assert e.source == "gmail"
        assert e.content_text == "Hello"

    def test_source_lowercased(self):
        e = RawEvent(
            source="GMAIL",
            source_event_id="x",
            occurred_at=datetime.utcnow(),
        )
        assert e.source == "gmail"

    def test_source_stripped(self):
        e = RawEvent(
            source="  telegram  ",
            source_event_id="x",
            occurred_at=datetime.utcnow(),
        )
        assert e.source == "telegram"

    def test_content_null_bytes_removed(self):
        e = RawEvent(
            source="gmail",
            source_event_id="x",
            occurred_at=datetime.utcnow(),
            content_text="hello\x00world",
        )
        assert e.content_text == "helloworld"

    def test_content_stripped(self):
        e = RawEvent(
            source="gmail",
            source_event_id="x",
            occurred_at=datetime.utcnow(),
            content_text="  hello  ",
        )
        assert e.content_text == "hello"

    def test_empty_source_rejected(self):
        with pytest.raises(ValidationError):
            RawEvent(source="", source_event_id="x", occurred_at=datetime.utcnow())

    def test_empty_source_event_id_rejected(self):
        with pytest.raises(ValidationError):
            RawEvent(source="gmail", source_event_id="", occurred_at=datetime.utcnow())

    def test_empty_content_allowed(self):
        # Иногда событие — это просто факт без текста (например fav button)
        e = RawEvent(
            source="instagram", source_event_id="x", occurred_at=datetime.utcnow(),
            content_text="",
        )
        assert e.content_text == ""


class TestDedupKey:
    def test_dedup_key_format(self):
        e = RawEvent(source="gmail", source_event_id="msg_123", occurred_at=datetime.utcnow())
        assert e.dedup_key == "gmail:msg_123"

    def test_dedup_key_uses_normalized_source(self):
        e = RawEvent(source="GMAIL", source_event_id="msg_123", occurred_at=datetime.utcnow())
        assert e.dedup_key == "gmail:msg_123"


class TestOutboundDetection:
    def test_not_outbound_by_default(self):
        e = RawEvent(source="gmail", source_event_id="x", occurred_at=datetime.utcnow())
        assert e.is_outbound is False

    def test_outbound_via_direction(self):
        e = RawEvent(
            source="gmail", source_event_id="x", occurred_at=datetime.utcnow(),
            metadata={"direction": "sent"},
        )
        assert e.is_outbound is True

    def test_outbound_via_from_me(self):
        e = RawEvent(
            source="telegram", source_event_id="x", occurred_at=datetime.utcnow(),
            metadata={"from_me": True},
        )
        assert e.is_outbound is True


class TestSignal:
    def test_valid_signal(self):
        s = Signal(type="task", summary="Срок завтра")
        assert s.type == "task"
        assert s.date is None

    def test_signal_with_date(self):
        s = Signal(
            type="event", summary="ДР Анны",
            date=datetime(2026, 11, 12),
        )
        assert s.date.year == 2026

    def test_signal_summary_too_long_rejected(self):
        with pytest.raises(ValidationError):
            Signal(type="task", summary="x" * 501)

    def test_signal_summary_empty_rejected(self):
        with pytest.raises(ValidationError):
            Signal(type="task", summary="")

    def test_signal_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            Signal(type="invalid-type", summary="x")  # type: ignore[arg-type]


class TestEntityHint:
    def test_valid_hint(self):
        h = EntityHint(type="person", identifier="yegorov@itstep.org", name="Дмитрий Егоров")
        assert h.identifier == "yegorov@itstep.org"

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            EntityHint(type="invalid", identifier="x")  # type: ignore[arg-type]


class TestTriageMetadata:
    def test_minimal(self):
        m = TriageMetadata(importance=75)
        assert m.importance == 75
        assert m.signals == []

    def test_importance_bounded(self):
        with pytest.raises(ValidationError):
            TriageMetadata(importance=101)
        with pytest.raises(ValidationError):
            TriageMetadata(importance=-1)

    def test_full_payload(self):
        m = TriageMetadata(
            importance=85,
            topics=["виза", "сроки"],
            people_mentioned=["Дмитрий Егоров"],
            signals=[Signal(type="task", summary="Срок завтра 12:00")],
            active_topic_matches=[{"topic": "виза", "confidence": 0.95}],
            needs_action=True,
            triaged_by_provider="cerebras",
            triaged_by_model="gpt-oss-120b",
        )
        assert len(m.signals) == 1
        assert m.needs_action is True
