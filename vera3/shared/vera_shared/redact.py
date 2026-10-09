"""Вычистить секреты из текста перед тем, как он уйдёт туда, где его прочтут другие.

Комната агентов читается всеми агентами владельца, а голосовое поручение —
распознанная речь: «пароль от сервера ку-два-шесть», продиктованный токен,
номер карты. До публикации всё похожее на секрет заменяется на `[скрыто]`.

Шаблоны — форма, а не энтропия: ключи с известным префиксом (sk-, sk_live_,
ghp_, AKIA, токен бота, JWT), длинные hex и base64, IBAN, пароль в URL,
слова после «пароль/password», «пин», «код из смс», любая группа из 13–19
цифр. Ложное срабатывание — потерянное слово в задаче; пропуск — секрет в
комнате. Поэтому номер карты чистится без проверки Луна: распознавание
путает цифры, и настоящая карта с одной ошибкой Луна уже не проходит.
Телефон с «+» и группы короче 13 цифр (суммы, даты, номера заказов) не
трогаются.
"""
from __future__ import annotations

import re

MASK = "[скрыто]"

_KEYS = re.compile(
    r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{16,}|[sprk]k_(?:live|test)_[A-Za-z0-9]{8,}"
    r"|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|\d{8,10}:AA[A-Za-z0-9_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]{10,})")
_URL_CREDS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s/]+)(@)", re.I)
_BEARER = re.compile(
    r"(?i)(\b(?:bearer|token|токен|ключ|key|secret|секрет|api_?key|access_?key))([\"']?\s*[:=]?\s*[\"']?)"
    r"([A-Za-z0-9._~+/=-]{12,})")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b", re.I)
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
# base64/urlsafe: длинный слиток с цифрами и буквами обоих регистров.
_B64 = re.compile(r"(?<![\w/+=-])(?=[A-Za-z0-9+/=_-]*\d)(?=[A-Za-z0-9+/=_-]*[a-z])"
                  r"(?=[A-Za-z0-9+/=_-]*[A-Z])[A-Za-z0-9+/=_-]{32,}")
_PASSWORD = re.compile(
    r"(?i)\b(парол[ьяюем]*|password|passwd|pwd|pass)(\s*(?:это|is|[:=—-])?\s*)(\S.*)")
#: Диктуют по слогам («ку два шесть») — берём до шести слов, но не дальше
#: знака конца фразы; знак сам остаётся в тексте.
PASSWORD_WORDS = 6
_PIN = re.compile(
    r"(?i)\b(пин-код|пин|pin(?:\s*code)?|код\s+из\s+(?:смс|sms)|смс-код|sms-?code"
    r"|код\s+подтверждения)(\s*[:=—-]?\s*)(\d(?:[ -]?\d){2,9})(?!\d)")
_CARD = re.compile(r"(?<![\d+])\d(?:[ -]?\d){12,18}(?![\d])")
_TRAILING = ".,;!?"


def _password(m: re.Match[str]) -> str:
    head, sep, rest = m.group(1), m.group(2), m.group(3)
    words = rest.split(" ")
    taken: list[str] = []
    for word in words[:PASSWORD_WORDS]:
        taken.append(word)
        if word.endswith(tuple(_TRAILING)) and len(word.rstrip(_TRAILING)) < len(word):
            break
    last = taken[-1]
    stripped = last.rstrip(_TRAILING)
    # Хвостовой знак — граница фразы, а не часть пароля: «Qwerty!23» — пароль
    # целиком, а «hunter2,» — пароль и запятая.
    tail = last[len(stripped):] if stripped else ""
    left = " ".join(words[len(taken):])
    return f"{head}{sep}{MASK}{tail}{' ' + left if left else ''}"


def redact_secrets(text: str) -> str:
    """Тот же текст, где всё похожее на секрет заменено на `[скрыто]`."""
    out = _KEYS.sub(MASK, text)
    out = _URL_CREDS.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
    out = _PASSWORD.sub(_password, out)
    out = _BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _PIN.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _IBAN.sub(MASK, out)
    out = _CARD.sub(MASK, out)
    out = _HEX.sub(MASK, out)
    return _B64.sub(MASK, out)
