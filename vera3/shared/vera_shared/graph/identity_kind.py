"""Person vs organization for e-mail senders: service mailboxes are not people."""
from __future__ import annotations

from vera_shared.graph.dupe_keys import FREE_MAIL

# ─── человек или организация ────────────────────────────────────────────────
# Gmail-ингестор раньше заводил КАЖДОГО отправителя как person, и граф людей
# засорялся сервисными ящиками: 145 «персон» вида no-reply@alerts.airasia.com,
# invoice+statements@mail.anthropic.com, crm@itstep.org — 134 из них вообще без
# единой связи. Они же плодили ложные «дубли»: одна организация с четырёх
# адресов = четыре одноимённые персоны (аудит 2026-08-06).
_SERVICE_LOCALPARTS = (
    "no-reply", "noreply", "donotreply", "do-not-reply", "notification",
    "notifications", "support", "info", "billing", "invoice", "alert",
    "alerts", "news", "newsletter", "marketing", "team", "hello", "contact",
    "admin", "crm", "logbook", "welcome", "mailer", "bounce", "postmaster",
    "feedback", "digest", "updates", "update", "reply", "noreply-app",
    "account", "accounts", "service", "help", "notify", "payment", "payments",
    "statements", "receipts", "reviews", "analytics", "webinar",
)
_SERVICE_SUBDOMAINS = ("mail.", "email.", "alert.", "alerts.", "notification.",
                       "notifications.", "update.", "updates.", "reply.",
                       "newsletter.", "communication.", "communications.",
                       "mailing.", "notificationmail.")


def entity_kind_for_email(addr: str | None) -> str:
    """'organization' для служебных ящиков, иначе 'person'.

    Служебным считаем адрес, у которого local-part начинается со служебного
    слова (плюс-адресация и разделители учитываются: `invoice+statements@`,
    `no_reply-2@`) либо домен начинается с сервисного поддомена. Адрес на
    free-mail домене (gmail.com…) — всегда человек: организацией по домену его
    не назвать, там тысячи несвязанных людей (аудит 2026-09-26).
    """
    if not addr or "@" not in addr:
        return "person"
    local, _, domain = addr.strip().lower().partition("@")
    if domain in FREE_MAIL:
        return "person"
    # Токены local-part: `ads-account-noreply` → [ads, account, noreply];
    # совпадение по ЦЕЛОМУ токену, чтобы `supportive-care` не считался
    # `support`, а `informationsecurity` — `info`.
    tokens = local.split("+")[0].replace("_", "-").replace(".", "-").split("-")
    if any(t in _SERVICE_LOCALPARTS for t in tokens):
        return "organization"
    if domain.startswith(_SERVICE_SUBDOMAINS):
        return "organization"
    return "person"
