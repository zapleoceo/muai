"""Password declarations in conversational text, not credential storage."""
from __future__ import annotations

import re

_LABEL = r"(?:password|passwd|pwd|пароль)"
_QUOTED = r'''(?:"[^"\r\n]{1,500}"|'[^'\r\n]{1,500}'|`[^`\r\n]{1,500}`|«[^»\r\n]{1,500}»)'''
_BARE = r'''[^\s"'`«»<>]{1,500}'''
_VALUE = rf"(?:{_QUOTED}|{_BARE})"
# A suffix declaration is just as explicit as `password: value`.
_SUFFIX = re.compile(
    rf"(?i)(?<!\S)(?P<value>{_VALUE})(?P<label>[ \t]+(?:"
    rf"вот[ \t]+такой[ \t]+пароль(?:[ \t]+введи)?"
    rf"|(?:это|мой|наш|ваш)[ \t]+пароль"
    rf"|is[ \t]+(?:the|my|your)[ \t]+password)\b)")
_PREFIX = re.compile(
    rf"(?i)(?P<label>(?<!\w){_LABEL}(?:[ \t]+(?:для|от|for)[ \t]+[\w.@-]+)?"
    rf"[ \t]*(?::|=|—|–|\bis\b)[ \t]*)(?P<value>{_VALUE})")
# Without a delimiter, require a quoted value or a token with a digit/symbol.
# This leaves ordinary statements such as 'пароль не нужен' untouched.
_SPACE = re.compile(
    rf"(?i)(?P<label>(?<!\w){_LABEL}[ \t]+)(?P<value>{_QUOTED}|"
    r'''(?=[^\s"'`«»<>]{0,499}[0-9!@#$%^&*+=_])[^\s"'`«»<>]{1,500})''')


def redact_passwords(text: str, mask: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group("value")
        if value == mask or len(value) < 4 or value.lower() in {"null", "none", "true", "false"}:
            return match.group(0)
        if match.group("label").strip().lower().endswith(" is") and value.isalpha():
            return match.group(0)
        start, end = match.span("value")
        offset = match.start()
        return match.group(0)[:start - offset] + mask + match.group(0)[end - offset:]

    out = _SUFFIX.sub(replace, text)
    out = _PREFIX.sub(replace, out)
    return _SPACE.sub(replace, out)
