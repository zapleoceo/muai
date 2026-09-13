"""Стоит ли вообще спрашивать LLM о связях в этом событии — без LLM.

Одна политика для всех путей запуска rel-extract (триаж, `reindex_rels.py`).
Чистые функции: решение принимается по источнику, метаданным и тексту
события, поэтому тестируется без базы и не расходится между путями.

## Почему гейт по данным, а не только по importance

Замер на проде 2026-09-13: rel-extract делал ~855 LLM-вызовов в неделю и с
2026-09-04 не создал ни одной связи. На 12 реальных событиях importance≥60
модели честно вернули `[]` — это новости, бот-уведомления, объявления,
переписка без отношений; на контрольных фразах со связями те же модели
отвечают правильно. То есть вызовы жглись там, где связей быть не может.

Распределение кандидатов (importance≥60, 30 дней → сколько событий дали
хоть одну связь): claude_chat 3776 → 10 (и все мусор: «Дима works_at
Trello», «Дима works_at GitHub»), telegram-группы 2514 → 16, gmail 471 → 28,
почти все от JIRA/календаря/рассылок («Ольга Крячко (JIRA) works_at Artem
Belov»). Признаки ниже отсекают именно эти классы.
"""
from __future__ import annotations

import re
from email.utils import parseaddr

from vera_shared.graph.identity import entity_kind_for_email
from vera_shared.ingest.envelope import message_body

#: Причины пропуска — пишутся в лог счётчиком, чтобы решение было видно.
SKIP_SOURCE = "source"
SKIP_CHANNEL = "channel"
SKIP_NO_PARTICIPATION = "no_participation"
SKIP_MACHINE_SENDER = "machine_sender"
SKIP_SHORT = "short"
SKIP_NO_MARKER = "no_marker"

MIN_BODY_CHARS = 30

# Транскрипты разговоров владельца с ассистентами — про код и инфраструктуру.
# 3776 кандидатов за 30 дней дали 10 событий со связями, все выдуманные.
_NO_RELATION_SOURCES = frozenset({"claude_chat", "vera_chat"})

# «Viktor Havrylenko (JIRA)» — человек в имени, но пишет система: связи из
# таких писем — «кто обновил задачу», а не кто на кого работает.
_TOOL_TAG_RE = re.compile(
    r"\((jira|confluence|github|gitlab|trello|slack|google calendar|"
    r"календарь|calendar)\)", re.IGNORECASE)
_TOOL_MAILBOXES = frozenset({"jira", "confluence", "calendar", "gitlab", "github"})

# Лексические маркеры отношений (ru/uk/en/id — языки реальных чатов). Без
# хотя бы одного такого слова LLM либо возвращает [], либо выдумывает связь
# из «X указан как гость встречи». Стемы с границей слова в начале, чтобы
# «обработка» не считалась «работой»; «друг» — только формами существительного,
# иначе ловится «другой».
_MARKERS = (
    # ru
    r"работ", r"трудоустр", r"уволи", r"нанял", r"наним", r"начальни",
    r"руковод", r"директор", r"босс", r"шеф\w{0,2}\b", r"подчин", r"сотрудни",
    r"коллег", r"основател", r"сооснов", r"совладел", r"партн[её]р", r"клиент",
    r"заказчик", r"подрядчик", r"поставщик", r"жен[аыеуо]й?\b", r"муж\w{0,2}\b",
    r"супруг", r"мам[аыеуо]й?\b", r"мат(ь|ер)", r"пап[аыеуо]й?\b", r"от(е|)ц",
    r"сын\w{0,2}\b", r"доч", r"брат\w{0,2}\b", r"сестр", r"друг(а|у|ом)?\b",
    r"друзь", r"подруг", r"жив[её]т в\b", r"живу в\b", r"переехал",
    r"зарплат", r"зп\b", r"оклад", r"менеджер", r"тимлид", r"команд[аеуы]",
    r"присоедин",
    # uk
    r"працю", r"керівни", r"колег", r"співробітни", r"засновни", r"дружин",
    r"чолові", r"батьк", r"донь", r"клієнт", r"замовни", r"живе в\b",
    # en
    r"works? (at|for|with)\b", r"working (at|for|with)\b", r"employ", r"hired",
    r"boss", r"manager", r"reports? to\b", r"colleague", r"co-?worker",
    r"co-?founder", r"founder", r"ceo\b", r"cto\b", r"cfo\b", r"partner",
    r"client", r"customer", r"vendor", r"supplier", r"contractor", r"wife",
    r"husband", r"spouse", r"mother", r"father", r"mom\b", r"dad\b", r"son\b",
    r"daughter", r"brother", r"sister", r"friend", r"girlfriend", r"boyfriend",
    r"lives? in\b", r"moved to\b", r"joined\b", r"team\b",
    # id
    r"bekerja", r"kerja di\b", r"atasan", r"rekan", r"istri", r"suami",
    r"teman", r"klien", r"pelanggan", r"bergabung", r"tim\b", r"bergabung", r"tim",
)
_MARKER_RE = re.compile(r"\b(?:" + "|".join(_MARKERS) + ")", re.IGNORECASE)


def has_relation_marker(text: str | None) -> bool:
    """Есть ли в тексте хоть одно слово, которым вообще называют отношения."""
    return bool(text) and _MARKER_RE.search(text) is not None


def is_machine_sender(metadata: dict | None) -> bool:
    """Автор события — система (рассылка, JIRA, календарь, бот), а не человек."""
    if not metadata:
        return False
    if metadata.get("is_bot") is True:
        return True
    username = str(metadata.get("sender_username") or "").lower()
    if username.endswith("bot"):
        return True
    raw_from = metadata.get("from")
    if not raw_from:
        return False
    name, addr = parseaddr(str(raw_from))
    if _TOOL_TAG_RE.search(name or ""):
        return True
    local = addr.lower().partition("@")[0]
    if local in _TOOL_MAILBOXES:
        return True
    return bool(addr) and entity_kind_for_email(addr) == "organization"


def rel_extract_skip_reason(source: str | None, metadata: dict | None,
                            body: str | None) -> str | None:
    """Почему по этому событию НЕ стоит звать rel-extract. None — стоит.

    Каналы и группы без участия владельца — прежняя логика: посты каналов
    (реклама, новости) дали `Дима -[client_of]-> T2T` из афиши SUP-тура
    (инцидент 2026-08-06), а чужая публичная болтовня личных фактов о
    владельце не несёт. `owner_participates` пишется на загрузке; у старых
    событий поля нет — для них остаётся только проверка канала.
    """
    if source in _NO_RELATION_SOURCES:
        return SKIP_SOURCE
    meta = metadata or {}
    if meta.get("chat_kind") == "channel":
        return SKIP_CHANNEL
    if meta.get("owner_participates") is False:
        return SKIP_NO_PARTICIPATION
    if is_machine_sender(meta):
        return SKIP_MACHINE_SENDER
    # Шапка ингестора («Chat: Веранда сотрудники») — не текст сообщения.
    message = message_body(body).strip()
    if len(message) < MIN_BODY_CHARS:
        return SKIP_SHORT
    if not has_relation_marker(message):
        return SKIP_NO_MARKER
    return None


def should_extract_relations(source: str | None, metadata: dict | None,
                             body: str | None) -> bool:
    """Звать ли rel-extract по этому событию."""
    return rel_extract_skip_reason(source, metadata, body) is None
