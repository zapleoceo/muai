"""Ключи для поиска дублей: имя без регистра/алфавита и домен без поддомена.

Чистые функции без БД — на них держатся и детектор (`dupe_detect`), и
ингесторы (организация заводится по домену, а не по адресу отправителя).
"""
from __future__ import annotations

import re
import unicodedata

_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e",
    "є": "ie", "ж": "z", "з": "z", "и": "i", "і": "i", "ї": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "c",
    "ш": "s", "щ": "s", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "iu",
    "я": "ia", "ё": "e",
}
# Латинские сочетания, у которых кириллица даёт одну букву: kh→х, zh→ж, ch→ч…
_DIGRAPHS = (("shch", "s"), ("sch", "s"), ("kh", "h"), ("zh", "z"), ("ch", "c"),
             ("sh", "s"), ("ts", "ts"), ("yu", "iu"), ("ya", "ia"), ("ye", "ie"),
             ("yo", "e"), ("ju", "iu"), ("ja", "ia"))
_UNIFY = str.maketrans({"y": "i", "j": "i", "w": "v", "h": "g", "c": "k", "q": "k"})
_DOUBLE = re.compile(r"(.)\1+")


def _latin(word: str) -> str:
    out = "".join(_CYR.get(ch, ch) for ch in word)
    for src, dst in _DIGRAPHS:
        out = out.replace(src, dst)
    return out.replace("ie", "e")  # Yegor и Егор: начальное «е» латиницей — «ye»


def _fold(name: str | None) -> str:
    folded = (name or "").casefold().replace("й", "и").replace("ё", "е")
    return "".join(ch for ch in unicodedata.normalize("NFKD", folded)
                   if not unicodedata.combining(ch))


def word_key(word: str) -> str:
    """Ключ одного слова — тот же транслит-фолд, что у `name_key`."""
    return _DOUBLE.sub(r"\1", _latin(_fold(word)).translate(_UNIFY))


def name_words(name: str | None) -> list[str]:
    """Ключи слов имени (≥2 букв) в порядке написания."""
    return [word_key(w) for w in re.split(r"[^a-zа-яёіїєґ]+", _fold(name)) if len(w) > 1]


def name_key(name: str | None) -> str:
    """Ключ полного имени: «Виктор Гавриленко» и «Viktor Gavrylenko» → один.

    Транслит ru/uk→латиница, y/i/j и g/h склеены, kh→h, двойные буквы схлопнуты,
    порядок слов не важен. Меньше двух слов → пустой ключ: одиночное имя («Саша»)
    слишком слабая улика, чтобы по нему что-то сливать.
    """
    keys = sorted(name_words(name))
    return " ".join(keys) if len(keys) >= 2 else ""


def plain_name(name: str | None) -> str:
    """Имя чата для сравнения: регистр и пробелы не должны разводить пару."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name or "")).strip().casefold()


# Домены, где адрес — это человек, а не организация.
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "mail.ru", "inbox.ru", "list.ru",
    "bk.ru", "yandex.ru", "yandex.com", "ya.ru", "rambler.ru", "ukr.net",
    "i.ua", "meta.ua", "gmx.com", "gmx.de", "web.de", "zoho.com", "tutanota.com",
    "qq.com", "163.com", "naver.com",
})

# Платформы рассылок и поддержки: на одном домене живут тысячи чужих компаний,
# так что «один домен = одна организация» там неверно.
SHARED_PLATFORMS = frozenset({
    "stripe.com", "zendesk.com", "brevosend.com", "sendgrid.net", "mailchimp.com",
    "mailgun.org", "amazonses.com", "trustpilotmail.com", "intercom.io",
    "hubspotemail.net", "mandrillapp.com", "planfix.com", "freshdesk.com",
    "atlassian.net", "salesforce.com", "sendinblue.com", "postmarkapp.com",
    "mcsv.net", "rsgsv.net", "substack.com", "typeform.com", "notion.so",
})

_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "edu", "ac", "go",
                           "or", "ne", "mil"})

_GENERIC_TOKENS = frozenset({"no", "reply", "do", "not", "noreply", "donotreply",
                             "notification", "notifications", "info", "support",
                             "team", "mail", "service", "admin", "billing", "hello",
                             "welcome", "news", "contact", "feedback", "help",
                             "alerts", "alert", "security", "updates", "orders"})
GENERIC_NAMES = frozenset({
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "do_not_reply",
    "info", "support", "notification", "notifications", "hello", "team", "admin",
    "billing", "mail", "service", "news", "newsletter", "welcome", "contact",
    "feedback", "help", "alerts", "alert", "security", "updates", "orders",
})


def email_domain(addr: str | None) -> str:
    value = (addr or "").strip().lower()
    return value.rsplit("@", 1)[1].strip(". ") if "@" in value else ""


def registrable_domain(domain: str | None) -> str:
    """`mail.anthropic.com` → `anthropic.com`, `x.go.id` → `x.go.id`.

    Без публичного списка суффиксов: два последних ярлыка, три — если
    предпоследний похож на второй уровень (`co`, `com`, `go`…) под двухбуквенной
    страной. Для почтовых отправителей этого достаточно.
    """
    labels = [p for p in (domain or "").strip().lower().strip(".").split(".") if p]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_generic_name(name: str | None, addr: str | None = None) -> bool:
    """Имя взято из local-part («noreply», «Notification noreply») или это сам
    адрес, а не название компании. Имя, совпавшее с local-part как бренд
    (`upwork@upwork.com` → «Upwork»), generic НЕ считается."""
    value = (name or "").strip().lower()
    if not value or "@" in value or value in GENERIC_NAMES:
        return True
    tokens = [t for t in re.split(r"[\s._\-]+", value) if t]
    return bool(tokens) and all(t in _GENERIC_TOKENS for t in tokens)


def alnum(value: str | None) -> str:
    return "".join(ch for ch in (value or "").casefold() if ch.isalnum())


def brand_fit(name: str | None, domain: str, addr: str | None = None) -> str:
    """Насколько имя подходит организации с этим доменом.

    `exact` — имя и есть бренд домена; `fits` — бренд входит в имя (или имя в
    бренд); `generic` — имя из local-part; `foreign` — что-то постороннее
    (имя живого человека в заголовке рассылки, другой бренд).
    """
    label, full, key = domain.split(".")[0], domain.replace(".", ""), alnum(name)
    if is_generic_name(name, addr):
        return "generic"
    if key in (label, full):
        return "exact"
    if len(label) >= 3 and (label in key or (len(key) >= 3 and key in label)):
        return "fits"
    return "foreign"


def domain_brand(domain: str) -> str:
    """`tuneprotect.com` → `Tuneprotect`: честнее, чем «noreply»."""
    label = registrable_domain(domain).split(".")[0]
    return label[:1].upper() + label[1:]


def org_domain(addr: str | None) -> str | None:
    """Домен организации-отправителя или None, если адрес личный (free-mail)."""
    domain = registrable_domain(email_domain(addr))
    return None if not domain or domain in FREE_MAIL else domain
