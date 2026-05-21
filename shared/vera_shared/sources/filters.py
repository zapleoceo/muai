"""Source filter engine.

Filter rule shape:
    {"match": {...predicates...}, "action": "include"|"exclude"|"priority"}

Predicates (all optional, AND-combined):
    chat_type:           "private" | "group" | "supergroup" | "channel"
    chat_id:             int or list[int]
    chat_id_in:          alias for chat_id list
    chat_id_not_in:      list[int] — negation
    folder:              telegram folder name
    from_user_id:        int or list[int]
    from_username:       string or list[string] (case-insensitive)
    from_contact_known:  true (sender is in contacts)
    mention_me:          true (message @mentions the userbot)
    reply_to_me:         true (reply to a message authored by the userbot)
    text_contains:       substring (case-insensitive)
    text_regex:          regex pattern
    from_contains:       substring in sender email/name (gmail)
    subject_contains:    substring in subject (gmail)
    time_of_day_between: ["HH:MM", "HH:MM"]
    has_attachment:      true

Evaluation: last matching rule wins. If no rule matches → default action
is "exclude" (do not ingest).
"""
import logging
import re
from datetime import datetime
from typing import Literal

_log = logging.getLogger(__name__)
_warned: set[str] = set()

FilterAction = Literal["include", "exclude", "priority"]
_DEFAULT: FilterAction = "exclude"


def _hour_between(now_hm: str, lo: str, hi: str) -> bool:
    """lo can be > hi to mean wrap around midnight."""
    if lo <= hi:
        return lo <= now_hm <= hi
    return now_hm >= lo or now_hm <= hi


def _to_set(v) -> set:
    if isinstance(v, (list, tuple, set)):
        return {x for x in v}
    return {v}


def _matches_one(match: dict, payload: dict) -> bool:
    for key, want in match.items():
        if key == "chat_type":
            if payload.get("chat_type") != want:
                return False
        elif key in ("chat_id", "chat_id_in"):
            if payload.get("chat_id") not in _to_set(want):
                return False
        elif key == "chat_id_not_in":
            if payload.get("chat_id") in _to_set(want):
                return False
        elif key == "folder":
            if (payload.get("folder") or "").lower() != str(want).lower():
                return False
        elif key == "from_user_id":
            if payload.get("from_user_id") not in _to_set(want):
                return False
        elif key == "from_username":
            uname = (payload.get("from_username") or "").lower()
            wants = {str(x).lower().lstrip("@") for x in _to_set(want)}
            if uname.lstrip("@") not in wants:
                return False
        elif key == "from_contact_known":
            if bool(payload.get("from_contact_known")) is not bool(want):
                return False
        elif key == "mention_me":
            if bool(payload.get("mention_me")) is not bool(want):
                return False
        elif key == "reply_to_me":
            if bool(payload.get("reply_to_me")) is not bool(want):
                return False
        elif key == "text_contains":
            haystack = (payload.get("text") or "").lower()
            if str(want).lower() not in haystack:
                return False
        elif key == "text_regex":
            try:
                if not re.search(str(want), payload.get("text") or "", re.IGNORECASE):
                    return False
            except re.error:
                return False
        elif key == "from_contains":
            haystack = (payload.get("from") or "").lower()
            if str(want).lower() not in haystack:
                return False
        elif key == "subject_contains":
            haystack = (payload.get("subject") or "").lower()
            if str(want).lower() not in haystack:
                return False
        elif key == "time_of_day_between":
            if not (isinstance(want, (list, tuple)) and len(want) == 2):
                return False
            now = (payload.get("now") or datetime.utcnow()).strftime("%H:%M")
            if not _hour_between(now, str(want[0]), str(want[1])):
                return False
        elif key == "has_attachment":
            if bool(payload.get("has_attachment")) is not bool(want):
                return False
        else:
            if key not in _warned:
                _warned.add(key)
                _log.warning("filters: unknown predicate key %r — rule will never match", key)
            return False
    return True


def evaluate(rules: list | None, payload: dict) -> FilterAction:
    """Walk rules, return action of the LAST matching one. Default exclude."""
    if not rules:
        return _DEFAULT
    last: FilterAction = _DEFAULT
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = rule.get("match") or {}
        action = rule.get("action") or "include"
        if action not in ("include", "exclude", "priority"):
            continue
        if _matches_one(match, payload):
            last = action  # type: ignore[assignment]
    return last
