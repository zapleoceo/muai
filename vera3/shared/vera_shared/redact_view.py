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
    rf"|xox[abprs]-[A-Za-z0-9-]{{10,255}}|xapp-[A-Za-z0-9-]{{10,255}}|sk-[A-Za-z0-9_-]{{16,255}}"
    rf"|glpat-[A-Za-z0-9_-]{{20,255}}|[sr]k_(?:live|test)_[A-Za-z0-9]{{16,255}}"
    rf"|AIza[0-9A-Za-z_-]{{35}}"
    rf"|(?:AKIA|ASIA)[0-9A-Z]{{16}}(?![A-Za-z0-9])"
    rf"|eyJ[A-Za-z0-9_-]{{5,2000}}\.eyJ[A-Za-z0-9_-]{{5,2000}}\.[A-Za-z0-9_-]{{5,2000}})")
# Бот-токен Telegram: допустим префикс `bot` в URL, поэтому якорь только по цифрам.
_TG = re.compile(r"(?<![0-9])[0-9]{6,12}:AA[A-Za-z0-9_-]{30,255}")
# NL: настоящий перевод строки или литерал "\n" (JSON). Без END (обрезанный превью)
# маскируются только заголовки и строки чисто из base64: проза после не съедается.
_NL = r"(?:\r?\n|\\n)"
_PEM = re.compile(
    r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----[ \t]*" + _NL + r"?"
    r"(?:[A-Za-z][A-Za-z0-9-]{0,30}:[^\r\n\\]{0,200}" + _NL + r"){0,6}"
    + _NL + r"?"
    r"(?:[A-Za-z0-9+/=]{1,4000}[ \t]*(?:" + _NL + r"|$)){0,400}"
    r"(?:-----END [A-Z ]{0,20}PRIVATE KEY-----)?")
_LABELS = r"token|secret|password|passwd|пароль|токен|секрет|api[_-]?key"
_VALUE_END = r"(?![^\s\"',;&])"
_LABEL = re.compile(
    rf"(?i)((?<![^\W_])(?<!(?-i:[A-Z0-9]_))(?:{_LABELS})[\"']?\s*[:=]\s*[\"']?)"
    r"(?!(?:null|none|true|false)(?![\w]))[^\s\"',;&]{4,500}")
# С лишними словами между меткой и «:» («token limit: 4096») фраза часто обычная
# речь: числа, URL и короткие строчные слова не маскируются.
_LABEL_GAP = re.compile(
    rf"(?i)((?<![^\W_])(?:{_LABELS})(?:[ \t]+[\w.@-]{{1,40}}){{1,3}}[\"']?\s*[:=]\s*[\"']?)"
    rf"(?!(?:null|none|true|false){_VALUE_END}|[0-9]+{_VALUE_END}|https?://"
    rf"|(?-i:[a-zа-яё]{{1,6}}){_VALUE_END})[^\s\"',;&]{{4,500}}")
_AUTH = re.compile(
    r"(?i)((?<![^\W_])authorization[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic|token)[ \t]+)?)"
    r"[^\s\"',;]{4,2000}")
_BEARER = re.compile(r"(?i)(\bbearer[ \t]+)[A-Za-z0-9._~+/=-]{16,2000}")
_ENV_TAIL = r"""[\"']?[ \t]*[:=][ \t]*[\"']?)"""
_ENV_VALUE = r"""[^\s\"',;&]{4,500}"""
_ENV_PREFIX = r"(?:[A-Z][A-Z0-9]*_)*"
# Числовое значение *_TOKEN (`MAX_TOKEN=4096`) - лимит, не секрет; числовой пароль
# или ключ (`DB_PASSWORD=12345678`) маскируется всегда.
_ENV = re.compile(
    rf"((?<![A-Za-z0-9_])(?:{_ENV_PREFIX}(?:SECRET(?:_KEY)?|PASSWORD|PASSWD|API_KEY)"
    rf"|aws_secret_access_key|passwd){_ENV_TAIL}{_ENV_VALUE}")
_ENV_TOKEN = re.compile(
    rf"((?<![A-Za-z0-9_]){_ENV_PREFIX}TOKEN{_ENV_TAIL}"
    rf"(?![0-9]+(?![^\s\"',;&])){_ENV_VALUE}")
_NAMES = (r"access[-_]?token|refresh[-_]?token|id[-_]?token|token|api[-_]?key|sig|signature"
          r"|secret|password|pwd|auth|client[-_]?secret|session|session[-_]?id"
          r"|x-amz-(?:signature|credential|security-token)")
_PARAM = re.compile(rf"(?i)((?<=[?&#;])(?:{_NAMES})=)[^&#\s\"'<>]+")
# `key=` в URL — и просто id страницы (`?key=page2`); маскируется лишь похожее на ключ.
_KEY_PARAM = re.compile(
    r"(?i)((?<=[?&#;])key=)(?:AIza[0-9A-Za-z_-]{35}|[A-Za-z0-9_-]{20,500})(?=[&#\s\"'<>]|$)")
_CHUNK = re.compile(r"[?#][^\s\"'<>]{1,4000}")
_CODE = re.compile(r"(?i)((?<=[?&#;])code=)[^&#\s\"'<>]+")
_OAUTH_CTX = re.compile(r"(?i)(?:^|[?&])(?:state|redirect_uri)=")
_USERINFO = re.compile(
    r"((?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,20}://[^\s:/@]{0,200}:)[^\s/@]{1,500}(?=@)")

# Ключ секретный, если после нормализации (camelCase -> snake, `-` -> `_`, нижний
# регистр) равен имени или кончается на `_<имя>` (граница сегмента): `x-api-key`,
# `accessToken`, `Set-Cookie` да; `token_count`, `max_tokens`, `secret_santa` нет.
_SECRET_NAMES = ("secret", "client_secret", "token", "access_token", "refresh_token",
                 "id_token", "api_key", "x_api_key", "apikey", "password", "passwd",
                 "private_key", "authorization", "cookie", "credentials")
_SECRET_SUFFIXES = tuple("_" + n for n in _SECRET_NAMES)
# Курсоры пагинации не секрет, хотя кончаются на `_token`.
_PAGING_KEYS = frozenset({"page_token", "next_page_token", "prev_page_token",
                          "continuation_token", "cursor"})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    norm = _CAMEL.sub("_", key).lower().replace("-", "_")
    if norm in _PAGING_KEYS:
        return False
    return norm in _SECRET_NAMES or norm.endswith(_SECRET_SUFFIXES)


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
    out = _TG.sub(MASK, out)
    for rx in (_AUTH, _BEARER, _ENV, _ENV_TOKEN, _LABEL, _LABEL_GAP, _PARAM, _KEY_PARAM):
        out = rx.sub(rf"\g<1>{MASK}", out)
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
        return {k: (_mask_leaves(v) if _is_secret_key(k)
                    else redact_tree(v)) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact_tree(v) for v in obj]
    return obj
