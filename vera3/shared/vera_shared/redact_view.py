"""Редакция выдачи: маскируется только значение секрета, остальной текст цел.

Отличие от `redact.py` (комната агентов, агрессивно, с потерей слов): здесь
высокая точность. Не трогаются hex 32/40/64, UUID, длинные числа, base64 без
метки (включая data: URI) и обычные URL с идентификаторами. Все шаблоны
линейны: якорь в начале прогона символов (lookbehind) и ограниченные классы.
"""
from __future__ import annotations

import re
from typing import Any

MASK = "[скрыто]"

_B = r"(?<![A-Za-z0-9_-])"
_KEYS = re.compile(
    rf"{_B}(?:gh[pousr]_[A-Za-z0-9]{{20,255}}|github_pat_[A-Za-z0-9_]{{20,255}}"
    rf"|xox[abprs]-[A-Za-z0-9-]{{10,255}}|sk-[A-Za-z0-9_-]{{16,255}}"
    rf"|(?:AKIA|ASIA)[0-9A-Z]{{16}}(?![A-Za-z0-9])"
    rf"|eyJ[A-Za-z0-9_-]{{5,2000}}\.eyJ[A-Za-z0-9_-]{{5,2000}}\.[A-Za-z0-9_-]{{5,2000}})")
# Без END (обрезанный превью) маскируется до конца base64-тела.
_PEM = re.compile(
    r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----[A-Za-z0-9+/=\s]{0,20000}"
    r"(?:-----END [A-Z ]{0,20}PRIVATE KEY-----)?")
_LABEL = re.compile(
    r"(?i)((?<![^\W_])(?:token|secret|password|пароль|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)"
    r"[^\s\"',;&]{8,500}")
_NAMES = (r"access[-_]?token|refresh[-_]?token|id[-_]?token|token|api[-_]?key|key|sig|signature"
          r"|secret|password|pwd|auth|client[-_]?secret|session|session[-_]?id"
          r"|x-amz-(?:signature|credential|security-token)")
_PARAM = re.compile(rf"(?i)((?<=[?&#;])(?:{_NAMES})=)[^&#\s\"'<>]+")
_CHUNK = re.compile(r"[?#][^\s\"'<>]{1,4000}")
_CODE = re.compile(r"(?i)((?<=[?&#;])code=)[^&#\s\"'<>]+")
_OAUTH_CTX = re.compile(r"(?i)(?:^|[?&])(?:state|redirect_uri)=")
_USERINFO = re.compile(
    r"((?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,20}://[^\s:/@]{1,200}:)[^\s/@]{1,500}(?=@)")

_SECRET_KEYS = frozenset({"authorization", "cookie", "password", "secret", "token", "api_key"})


def _mask_code(m: re.Match[str]) -> str:
    chunk = m.group(0)
    if chunk[0] == "#" or _OAUTH_CTX.search(chunk):
        return _CODE.sub(rf"\g<1>{MASK}", chunk)
    return chunk


def redact_view(text: str) -> str:
    if not text:
        return text
    out = _PEM.sub(MASK, text)
    out = _KEYS.sub(MASK, out)
    out = _LABEL.sub(rf"\g<1>{MASK}", out)
    out = _PARAM.sub(rf"\g<1>{MASK}", out)
    out = _CHUNK.sub(_mask_code, out)
    return _USERINFO.sub(rf"\g<1>{MASK}", out)


def _mask_leaves(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _mask_leaves(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_leaves(v) for v in obj]
    if isinstance(obj, str | int | float) and not isinstance(obj, bool) and obj != "":
        return MASK
    return obj


def redact_tree(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_view(obj)
    if isinstance(obj, dict):
        return {k: (_mask_leaves(v) if isinstance(k, str) and k.lower() in _SECRET_KEYS
                    else redact_tree(v)) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact_tree(v) for v in obj]
    return obj
