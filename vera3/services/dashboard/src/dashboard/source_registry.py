"""Каталог источников — данные, а не разметка.

До 2026-08-26 страница `/sources` набиралась вручную, по блоку HTML на
источник. Trello, добавленный днём раньше, своего блока так и не получил и был
виден только строкой в общем списке: ревизия зафиксировала это как уже
случившийся пропуск, а не как гипотетическое нарушение открытости-закрытости.

Теперь новый источник добавляет здесь ОДНУ запись. Страница-список и страница
подробностей строятся из этого каталога, разметки под конкретный источник в
маршрутах нет.

`detail` — имя провайдера разбивок из `dashboard.source_detail`. Его может не
быть: тогда на странице источника видны состояние подключения и объём, а
специфичных разбивок нет — это нормально и честнее пустого блока.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    key: str                       # значение events.source
    title: str
    icon: str
    how: str                       # как получаем данные, одной строкой
    #: минуты, после которых поток считается тихим / замолчавшим. None —
    #: источник событийный и «свежести» у него нет (внутренние, one-shot).
    live_min: int | None = None
    warn_min: int | None = None
    connect_url: str | None = None
    connect_label: str | None = None
    detail: str | None = None      # ключ провайдера разбивок
    note: str = ""


CATALOG: tuple[Source, ...] = (
    Source(
        key="telegram", title="Telegram", icon="✈️",
        how="userbot MTProto, поток в реальном времени",
        live_min=5, warn_min=60,
        connect_url="/api/telegram/start", connect_label="Переподключить",
        detail="telegram",
    ),
    Source(
        key="gmail", title="Gmail", icon="📧",
        how="OAuth + опрос API раз в 5 минут",
        live_min=15, warn_min=180,
        connect_url="/api/gmail/start", connect_label="Переподключить",
        detail="gmail",
    ),
    Source(
        key="slack", title="Slack", icon="💬",
        how="Web API, опрос каналов и тредов раз в 5 минут",
        live_min=15, warn_min=180,
        connect_url="/api/slack/start", connect_label="Подключить",
        detail="slack",
        note="Ответы в тредах приходят отдельным обходом — history их не отдаёт.",
    ),
    Source(
        key="instagram", title="Instagram", icon="📸",
        how="instagrapi, опрос личных сообщений",
        live_min=10, warn_min=120,
        connect_url="/api/instagram/start", connect_label="Подключить",
        detail="instagram",
    ),
    Source(
        key="trello", title="Trello", icon="📋",
        how="REST-опрос действий досок + суточный дайджест сроков",
        live_min=15, warn_min=180,
        detail="trello",
        note="Ключ и токен — в infra/.env, в БД их нет.",
    ),
    Source(
        key="voice", title="Разговоры у ноутбука", icon="🎙",
        how="vera-listener на ноутбуке → POST /v1/voice/session",
        detail="voice",
        note="В мозг уходит выжимка, дословная расшифровка остаётся на ноутбуке.",
    ),
    Source(
        key="vera_chat", title="Диалог с Верой", icon="💭",
        how="бот пишет сюда вопросы владельца и свои ответы",
    ),
    Source(
        key="vera_memory", title="Память агента", icon="🧠",
        how="агент сохраняет выведенные факты сам",
    ),
    Source(
        key="claude", title="Claude", icon="🤖",
        how="факты из разговоров с Claude через MCP",
    ),
    Source(
        key="perplexity", title="Perplexity", icon="🔎",
        how="разовый импорт экспорта, scripts/import_perplexity.py",
    ),
)

BY_KEY: dict[str, Source] = {s.key: s for s in CATALOG}


def unlisted(key: str) -> Source:
    """Источник, которого нет в каталоге, но события от него в базе есть.

    Показывать его надо: скрыть — значит соврать про содержимое мозга. Такие
    строки в списке и есть сигнал «пора добавить запись в каталог».
    """
    return Source(key=key, title=key, icon="•", how="нет записи в каталоге")


def resolve_source(key: str) -> Source:
    return BY_KEY.get(key) or unlisted(key)
