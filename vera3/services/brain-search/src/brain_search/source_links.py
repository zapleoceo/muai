"""Expose a stored Slack permalink only when it names the retrieved message."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

_SLACK_ID = re.compile(r"^(?P<channel>[CGD][A-Z0-9]+):(?P<seconds>\d{10})\.(?P<fraction>\d{6})$")
_SLACK_HOST = re.compile(r"^[a-z0-9-]+\.slack\.com$")


def source_url(source: str, source_event_id: str, permalink: str | None = None) -> str | None:
    """Return an existing matching permalink; never construct one from IDs.

    Slack URLs can depend on the workspace and thread. The stored channel and
    timestamp are used only to reject a malformed or mismatched permalink.
    """
    if source != "slack" or not isinstance(permalink, str):
        return None
    match = _SLACK_ID.fullmatch(source_event_id)
    if match is None:
        return None
    try:
        url = urlsplit(permalink)
        host = url.hostname
    except ValueError:
        return None
    if (url.scheme != "https" or host is None or
            _SLACK_HOST.fullmatch(host) is None or
            url.netloc.lower() != host or url.fragment):
        return None
    expected_path = f"/archives/{match['channel']}/p{match['seconds']}{match['fraction']}"
    if url.path != expected_path:
        return None
    if url.query:
        try:
            params = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return None
        if (len(params) != 2 or {key for key, _ in params} != {"thread_ts", "cid"} or
                dict(params)["cid"] != match["channel"] or
                re.fullmatch(r"\d{10}\.\d{6}", dict(params)["thread_ts"]) is None):
            return None
    return permalink
