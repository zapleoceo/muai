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

#: Разбег одной и той же речи между дорожками. Было 2.5 с — мало: дорожки
#: режутся на куски независимо, и в записи разговора от 31.08 та же фраза
#: приходила с микрофона на 3.5 с позже, чем из loopback. Такие реплики
#: фильтр пропускал, и голос собеседника оседал в дорожке владельца.
WINDOW_S = 6.0

#: Порог для реплик сопоставимой длины.
RATIO = 0.75

#: Порог для ВХОЖДЕНИЯ одной реплики в другую. Микрофон часто ловит фразу
#: вместе с продолжением: loopback даёт «И дальше мои студенты должны иметь
#: возможность», микрофон — ту же фразу плюс ещё полтора предложения. Совпадение
#: по SequenceMatcher тут падает до ~0.6 (метрика делит на сумму длин), и полное
#: сравнение эхо не видит, хотя одна строка буквально содержит другую.
#:
#: Короткие реплики по вхождению не судим: «Да» или «Ага» содержатся почти в
#: любой длинной фразе, и владелец лишился бы собственных ответов. Порог в 16
#: символов отсекает и случайные совпадения вроде «давай перенесём»: в замере
#: на реальной записи все пойманные вхождения были заметно длиннее.
CONTAINED_MIN_CHARS = 16

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+", re.UNICODE)


def normalize(text: str) -> str:
    """К нижнему регистру, без знаков, с одиночными пробелами.

    Пробелы схлопываются именно ради проверки вхождения: без этого «а  б»
    не находится в «а б в», хотя на слух это одно и то же.
    """
    return _SPACES.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def similar(a: str, b: str) -> float:
    left, right = normalize(a), normalize(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def looks_like_echo(mic_text: str, system_text: str, *, ratio: float = RATIO) -> bool:
    """Одна ли это речь: либо строки почти равны, либо одна содержит другую."""
    if similar(mic_text, system_text) >= ratio:
        return True
    short, long = sorted((normalize(mic_text), normalize(system_text)), key=len)
    return len(short) >= CONTAINED_MIN_CHARS and short in long


def mark_echo(utterances: list[dict[str, Any]], *, window_s: float = WINDOW_S,
              ratio: float = RATIO) -> list[dict[str, Any]]:
    """Ставит `echo: True` микрофонным репликам, повторяющим loopback.

    Помечаем, а не выбрасываем. В один кусок микрофона попадает и эхо, и слова
    самого владельца: «…всем им нужна площадка через которую это делать я
    помню» — до «я помню» говорит собеседник из динамиков, дальше владелец.
    Выброс уносил бы и его слова, а звук мы не храним, значит потерянное не
    восстановить ничем. Нашло ревью.

    Дальше по помете расходятся два потребителя: дословная стенограмма берёт
    ВСЁ, осмысление — только непомеченное.
    """
    system = [u for u in utterances if u.get("stream") == "system"]
    out: list[dict[str, Any]] = []
    for utt in utterances:
        if utt.get("stream") != "mic" or not system:
            out.append(utt)
            continue
        at = float(utt.get("at", 0.0))
        echo = any(
            # Окно проверяем первым: сравнение строк дороже, и за пределами
            # окна его считать незачем.
            abs(float(other.get("at", 0.0)) - at) <= window_s
            and looks_like_echo(str(utt.get("text", "")), str(other.get("text", "")),
                                ratio=ratio)
            for other in system
        )
        out.append({**utt, "echo": True} if echo else utt)
    return out


def drop_echo(utterances: list[dict[str, Any]], *, window_s: float = WINDOW_S,
              ratio: float = RATIO) -> list[dict[str, Any]]:
    """Только непомеченные реплики — то, что уходит в осмысление."""
    return [u for u in mark_echo(utterances, window_s=window_s, ratio=ratio)
            if not u.get("echo")]
