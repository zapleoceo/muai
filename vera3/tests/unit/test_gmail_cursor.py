"""Gmail: курсор опроса и разбор тела письма.

`after:YYYY/MM/DD` считается в поясе Gmail, а не в UTC, в котором лежит
last_polled_at: письма между UTC-полуночью и полуночью Gmail выпадали из
выборки навсегда."""
from __future__ import annotations

import base64
import logging
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ingestor-gmail", "src"))

os.environ.setdefault("GMAIL_CLIENT_ID", "test-cid")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "test-csec")

from ingestor_gmail import poller  # noqa: E402


def _enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


class TestCursorQuery:
    def test_first_run_uses_relative_window(self):
        assert poller.build_query(None) == "newer_than:7d"

    def test_cursor_is_epoch_seconds_with_overlap(self):
        last = datetime(2026, 10, 3, 0, 30)   # naive UTC, как в БД
        epoch = int(poller.build_query(last).removeprefix("after:"))
        expected = int(datetime(2026, 10, 3, 0, 30, tzinfo=UTC).timestamp())
        assert epoch == expected - int(poller.CURSOR_OVERLAP.total_seconds())

    def test_window_reaches_back_past_utc_midnight(self):
        """Опрос в 00:30 UTC, письмо в 03:00 UTC: окно обязано его покрыть."""
        window_start = int(poller.build_query(datetime(2026, 10, 3, 0, 30))
                           .removeprefix("after:"))
        arrived = int(datetime(2026, 10, 3, 3, 0, tzinfo=UTC).timestamp())
        assert window_start < arrived


class TestExtractText:
    def test_bad_base64_logs_message_id_and_keeps_empty_body(self, caplog):
        payload = {"mimeType": "text/plain", "body": {"data": "a"}}
        with caplog.at_level(logging.WARNING, logger="gmail"):
            assert poller._extract_text(payload, "msg-42") == ""
        assert any("msg-42" in r.message for r in caplog.records)

    def test_plain_decodes(self):
        payload = {"mimeType": "text/plain", "body": {"data": _enc("привет")}}
        assert poller._extract_text(payload) == "привет"

    def test_html_part_is_stripped(self):
        html = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": _enc("<p>hi <b>there</b></p>")}}]}
        assert poller._extract_text(html) == "hi there"
