"""Resolve original message links only when the stored source ID is sufficient."""
from __future__ import annotations

import re

_SLACK_ID = re.compile(r"^(?P<channel>[CGD][A-Z0-9]+):(?P<seconds>\d{10})\.(?P<fraction>\d{6})$")


def source_url(source: str, source_event_id: str) -> str | None:
    """Return a source link, or None when a reliable link cannot be derived.

    Slack stores channel ID and message timestamp together. Other sources need
    account-specific link resolution; inventing a URL from an event ID would
    mislead the reader.
    """
    if source != "slack":
        return None
    match = _SLACK_ID.fullmatch(source_event_id)
    if match is None:
        return None
    return (f"https://app.slack.com/archives/{match['channel']}"
            f"/p{match['seconds']}{match['fraction']}")
