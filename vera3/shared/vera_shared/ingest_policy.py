"""Кого не пускаем в мозг вообще — денай-лист отправителей.

Отличие от `media_policy`: там решается, распознавать ли КАРТИНКУ у события,
которое всё равно сохраняется. Здесь — событие не сохраняется совсем.

Повод (2026-07-31, Дима: «@VerandamyBot исключи из мозга вообще»):
служебные боты сыпят машинными уведомлениями в рабочие чаты (6050 событий
в «Веранда сотрудники», «VerandaBot», «Старшие и отчеты»). Для памяти
человека это шум: он раздувает базу, ломает поиск и жжёт бюджет триажа,
а сами факты и так есть в системе-источнике.

2026-08-02 добавлен `@leomatchbot` (Дайвінчик, бот знакомств): ~800
анкетных фото в сутки — очередь распознавания росла быстрее, чем vision
вообще способен её разбирать. Здесь понадобился игнор ЧАТА, а не только
отправителя: исходящие в этом чате — 👎-свайпы самого Димы, фильтр по
отправителю их бы не поймал.

Фильтруем по username — он стабилен, в отличие от имени; для чата ещё и
по началу названия (у ботов username есть не всегда).
"""
from __future__ import annotations

# username'ы БЕЗ «@», в нижнем регистре.
_IGNORED_SENDER_USERNAMES = frozenset({
    "verandamybot",
    "leomatchbot",       # Дайвінчик — бот знакомств, ~800 анкетных фото/сутки
})

# Чаты целиком (по username собеседника или началу названия, нижний регистр).
# Нужно там, где мусор льют ОБЕ стороны: в переписке с ботом знакомств
# исходящие — это 👎-свайпы, входящие — анкеты. Фильтр по отправителю ловит
# только половину, потому что свайпы отправлены самим Димой.
_IGNORED_CHAT_USERNAMES = frozenset({
    "leomatchbot",
})
_IGNORED_CHAT_TITLE_PREFIXES = (
    "дайвінчик",
    "дайвинчик",
)


# Каналы Slack, которые в мозг не идут никогда. Slack — самая ботовая среда из
# подключённых: CI, алерты, дежурные уведомления, Zapier. Фильтр по `bot_id`
# (mapper.is_noise) снимает машинные сообщения, но в таких каналах и люди пишут
# машинным — «раскатил», «откатил», «зелёное». Для личной памяти это шум, а
# факты и так есть в системе-источнике. Ровно та же логика, что с уровнем чата
# для @leomatchbot: мусорят обе стороны, фильтра по автору мало.
_IGNORED_SLACK_CHANNELS = frozenset({
    "alerts", "alarms", "ci", "cicd", "builds", "deploys", "deployments",
    "monitoring", "sentry", "logs", "status", "uptime", "github", "gitlab",
    "jira-feed", "zapier", "integrations", "notifications",
})


def is_ignored_sender(username: str | None) -> bool:
    """True — сообщения этого отправителя в мозг не пишем."""
    if not username:
        return False
    return username.strip().lstrip("@").lower() in _IGNORED_SENDER_USERNAMES


def is_ignored_chat(chat_username: str | None, chat_title: str | None = None) -> bool:
    """True — чат целиком в мозг не пишем (обе стороны переписки)."""
    if chat_username and chat_username.strip().lstrip("@").lower() in _IGNORED_CHAT_USERNAMES:
        return True
    return bool(chat_title
                and chat_title.strip().lower().startswith(_IGNORED_CHAT_TITLE_PREFIXES))


def is_ignored_slack_channel(name: str | None, extra: frozenset[str] | None = None) -> bool:
    """True — канал Slack целиком в мозг не пишем.

    `extra` — добавка из SLACK_DENY_CHANNELS: набор шумных каналов зависит от
    воркспейса, а базовый список тут покрывает то, что называется одинаково
    почти везде.
    """
    if not name:
        return False
    clean = name.strip().lstrip("#").lower()
    if clean in _IGNORED_SLACK_CHANNELS:
        return True
    return bool(extra and clean in extra)
