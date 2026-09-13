"""Чистые метрики пилотов моделей: близость OCR к эталону, числа, WER.

Без зависимостей кроме stdlib — чтобы юнит-тест шёл в общем venv тестов.
"""
from __future__ import annotations

import re
import unicodedata

# Разделитель тысяч — только перед ровно тремя цифрами, иначе «12 34» из
# соседних ячеек склеилось бы в одно число.
_NUM_RE = re.compile(r"\d{1,3}(?:[ .,]\d{3})+(?!\d)|\d+(?:[.,:]\d+)*")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_MARKUP_RE = re.compile(r"[*#|`_>~]+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    text = _MARKUP_RE.sub(" ", text)
    return " ".join(text.split())


def gemini_ocr_part(reference: str) -> str:
    # Эталон gemini — «описание… Текст: <дословно>». Сравниваем только
    # дословную часть: описание модель-OCR не пишет и писать не должна.
    _, sep, tail = reference.partition("Текст:")
    return tail.strip() if sep else reference.strip()


def levenshtein(a: list[str] | str, b: list[str] | str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def char_similarity(hyp: str, ref: str) -> float:
    h, r = normalize(hyp), normalize(ref)
    if not h and not r:
        return 1.0
    return 1.0 - levenshtein(h, r) / max(len(h), len(r))


def token_recall(hyp: str, ref: str) -> float:
    ref_tokens = _WORD_RE.findall(normalize(ref))
    if not ref_tokens:
        return 1.0
    pool: dict[str, int] = {}
    for t in _WORD_RE.findall(normalize(hyp)):
        pool[t] = pool.get(t, 0) + 1
    hit = 0
    for t in ref_tokens:
        if pool.get(t, 0) > 0:
            pool[t] -= 1
            hit += 1
    return hit / len(ref_tokens)


def numbers(text: str) -> set[str]:
    # «1.250.000», «1,250,000» и «1 250 000» — одна сумма: сравниваем цифры.
    out = set()
    for m in _NUM_RE.findall(text):
        digits = re.sub(r"\D", "", m)
        if len(digits) >= 2:
            out.add(digits)
    return out


def number_recall(hyp: str, ref: str) -> float | None:
    ref_nums = numbers(ref)
    if not ref_nums:
        return None
    return len(ref_nums & numbers(hyp)) / len(ref_nums)


def wer(hyp: str, ref: str) -> float:
    r = _WORD_RE.findall(normalize(ref).replace("ё", "е"))
    h = _WORD_RE.findall(normalize(hyp).replace("ё", "е"))
    if not r:
        return 0.0 if not h else 1.0
    return levenshtein(h, r) / len(r)
