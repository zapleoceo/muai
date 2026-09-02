"""Имя собеседника из контекста приложения — для звонков один на один.

Зачем: разговор один на один в Slack или Telegram называет собеседника прямо
в заголовке окна. Это единственный источник настоящего ИМЕНИ, доступный
бесплатно — по звуку можно лишь отличить голоса друг от друга, но не узнать,
как человека зовут. Отсюда и связка: имя из заголовка → отпечаток голоса →
узнавание того же человека в общем созвоне, где имён не даёт никто.

## Почему у имени есть признак `is_direct`

Назвать реплику в текущем разговоре и ЗАПОМНИТЬ голос под именем — разные по
цене решения. Первое живёт до конца разговора и видно глазами. Второе уходит
в постоянное хранилище, и один раз ошибившись, слушатель будет годами звать
чужим именем.

Разница нужна из-за Telegram: у него в заголовке просто имя чата, и «Оля
Кричко» неотличимо от «Кайфушники Нячанга» — обе строки выглядят как имя
человека (проверено тестом, регулярка тут бессильна). Поэтому Telegram даёт
имя для разговора, но не даёт права запомнить голос.

Открыто для расширения: новое приложение — новое правило в RULES, старые не
трогаются.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

#: Похоже на имя человека: два-три слова с заглавных, без цифр и служебных
#: знаков. Отсекает названия каналов («general», «dev-team») и тем встреч
#: («статус апдейт»), но НЕ отличает человека от группы из двух слов.
_PERSON = re.compile(
    r"^[A-ZА-ЯЁ][\w'’-]+(?:\s+[A-ZА-ЯЁ][\w'’-]+){1,2}$", re.UNICODE)

#: Хвосты, которые Slack и Telegram дописывают к заголовку.
_NOISE = re.compile(
    r"\s*\((?:DM|ЛС)\)|\s*[-–—]\s*\d+\s+new\s+items?|\s*\(\d+\)", re.IGNORECASE)


@dataclass(frozen=True)
class Counterpart:
    """Кто на том конце — и можно ли доверять этому настолько, чтобы запомнить."""

    name: str
    #: True — приложение подтвердило разговор один на один, голос можно
    #: связать с именем навсегда. False — имя годится только для разметки
    #: этого разговора.
    is_direct: bool


def looks_like_person(text: str) -> bool:
    """Похоже ли на имя человека, а не на канал, тему встречи или мусор.

    Группу из двух слов не отличает — на это есть `Counterpart.is_direct`.
    """
    cleaned = _NOISE.sub("", text).strip()
    return bool(_PERSON.match(cleaned)) and not cleaned.startswith("#")


def _clean(text: str) -> str:
    return _NOISE.sub("", text).strip()


def _from_slack(title: str) -> Counterpart | None:
    """`Viktor Gavrylenko (DM) - Sintegrum Team - Slack` → имя, один на один.

    Slack кладёт собеседника или канал первым сегментом, и канал отличим по
    форме: у каналов имена вроде `general` или `dev-team`. Значит имя,
    прошедшее проверку формы, — это личная переписка, а не группа.
    """
    cleaned = _clean(title.split(" - ")[0])
    if not looks_like_person(cleaned):
        return None
    return Counterpart(name=cleaned, is_direct=True)


def _from_telegram(title: str) -> Counterpart | None:
    """У Telegram заголовок — имя чата, и группу от человека не отличить.

    «Оля Кричко» и «Кайфушники Нячанга» выглядят одинаково. Имя отдаём для
    разметки разговора, но `is_direct=False`: запоминать голос под ним
    нельзя, иначе группа однажды станет «человеком».
    """
    cleaned = _clean(title)
    if not looks_like_person(cleaned):
        return None
    return Counterpart(name=cleaned, is_direct=False)


def _from_meet(title: str) -> Counterpart | None:
    """Meet имени не даёт никогда — ни в один-на-один, ни в группе.

    В заголовке либо просто `Meet`, либо название встречи («Оркестра —
    статус апдейт»). Принять его за имя значило бы записать голос человека
    под названием совещания. Поэтому — всегда None, и это не заглушка.
    """
    return None


@dataclass(frozen=True)
class Rule:
    """Приложение → как достать имя из его заголовка."""

    app: str
    marker: str
    extract: Callable[[str], Counterpart | None]


#: `marker` — хвост, которым приложение подписывает свой заголовок. Проверяем
#: его ОТДЕЛЬНО от имени процесса, потому что они расходятся: звук может идти
#: из Telegram, пока впереди окно Slack (видели вживую 2026-09-02). Имя берём,
#: только когда и процесс, и заголовок говорят об одном приложении.
RULES: tuple[Rule, ...] = (
    Rule(app="slack.exe", marker="- Slack", extract=_from_slack),
    Rule(app="telegram.exe", marker="", extract=_from_telegram),
    Rule(app="chrome.exe", marker="Google Chrome", extract=_from_meet),
    Rule(app="msedge.exe", marker="Edge", extract=_from_meet),
)


def counterpart(app: str | None, window_title: str | None) -> Counterpart | None:
    """Кто на том конце, если приложение это назвало и это точно человек.

    None — обычный ответ, а не сбой: у Meet имён нет вовсе, у групповых
    каналов в заголовке название группы, а звук и окно вообще могут быть от
    разных приложений.
    """
    if not app or not window_title:
        return None
    app_name = app.strip().lower()
    for rule in RULES:
        if rule.app != app_name:
            continue
        if rule.marker and rule.marker not in window_title:
            # Заголовок не от этого приложения: впереди чужое окно. Гадать,
            # кто на самом деле на связи, нельзя — молчим.
            return None
        return rule.extract(window_title)
    return None


def counterpart_name(app: str | None, window_title: str | None) -> str | None:
    """Только имя, без признака доверия — для разметки текущего разговора."""
    found = counterpart(app, window_title)
    return found.name if found else None
