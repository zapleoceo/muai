"""Вычистить секреты из текста перед тем, как он уйдёт туда, где его прочтут другие.

Комната агентов читается всеми агентами владельца, а голосовое поручение —
распознанная речь: «пароль от сервера ку-два-шесть», продиктованный токен,
номер карты. До публикации всё похожее на секрет заменяется на `[скрыто]`.

Шаблоны — форма, а не энтропия: ключи с известным префиксом (sk-, ghp_,
AKIA, токен бота), длинные hex и base64, номер карты с проверкой Луна и
слова после «пароль/password». Ложное срабатывание — потерянное слово в
задаче; пропуск — секрет в комнате. Поэтому длинные hex/base64 режутся
без разбора, что они такое.
"""
from __future__ import annotations

import re

MASK = "[скрыто]"

_KEYS = re.compile(
    r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|\d{8,10}:AA[A-Za-z0-9_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]{10,})")
_BEARER = re.compile(r"(?i)\b(bearer|token|токен|ключ|key)(\s*[:=]?\s*)([A-Za-z0-9._~+/=-]{12,})")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
# base64/urlsafe: длинный слиток с цифрами и буквами обоих регистров.
_B64 = re.compile(r"(?<![\w/+=-])(?=[A-Za-z0-9+/=_-]*\d)(?=[A-Za-z0-9+/=_-]*[a-z])"
                  r"(?=[A-Za-z0-9+/=_-]*[A-Z])[A-Za-z0-9+/=_-]{32,}")
#: «пароль … » — до конца фразы, но не дальше шести слов: диктуют по слогам.
_PASSWORD = re.compile(
    r"(?i)\b(парол[ьяюем]*|password|passwd|pwd|pass)(\s*(?:это|is|[:=—-])?\s*)"
    r"([^\s.,;!?]+(?:\s+[^\s.,;!?]+){0,5})")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group())
    return MASK if 13 <= len(digits) <= 19 and _luhn(digits) else m.group()


def redact_secrets(text: str) -> str:
    """Тот же текст, где всё похожее на секрет заменено на `[скрыто]`."""
    out = _KEYS.sub(MASK, text)
    out = _PASSWORD.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _CARD.sub(_card, out)
    out = _HEX.sub(MASK, out)
    return _B64.sub(MASK, out)
