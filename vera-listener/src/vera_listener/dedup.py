"""Снятие эха: в динамиках собеседник попадает и в микрофон тоже.

Аппаратный AEC решает это в большинстве случаев, но не всегда и не сразу
после смены устройства. Второй слой — текстовый: реплика микрофона, почти
совпавшая по времени и словам с репликой из приложения, выбрасывается.
Выбрасывается именно микрофонная: удалённую сторону вернее слышит loopback.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

WINDOW_S = 2.5
RATIO = 0.75
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)


def normalize(text: str) -> str:
    return _PUNCT.sub(" ", text.lower()).strip()


def similar(a: str, b: str) -> float:
    left, right = normalize(a), normalize(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def drop_echo(utterances: list[dict[str, Any]], *, window_s: float = WINDOW_S,
              ratio: float = RATIO) -> list[dict[str, Any]]:
    """Убирает микрофонные повторы того, что уже сказано в loopback."""
    system = [u for u in utterances if u.get("stream") == "system"]
    if not system:
        return list(utterances)

    kept: list[dict[str, Any]] = []
    for utt in utterances:
        if utt.get("stream") != "mic":
            kept.append(utt)
            continue
        at = float(utt.get("at", 0.0))
        echo = any(
            abs(float(other.get("at", 0.0)) - at) <= window_s
            and similar(str(utt.get("text", "")), str(other.get("text", ""))) >= ratio
            for other in system
        )
        if not echo:
            kept.append(utt)
    return kept
