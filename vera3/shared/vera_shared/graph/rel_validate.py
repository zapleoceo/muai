"""Проверка связи здравым смыслом ДО записи в граф — чистая функция.

Та же функция прогоняет по графу уже записанные связи
(`scripts/quarantine_junk_rels.py`), поэтому правило одно на запись и на
чистку.

## Откуда правила

До 2026-09-03 структурные вызовы обслуживал в основном mistral-small, и он
наплодил связей, которые ломаются на типах концов: «Link works_at OpenRouter,
Inc» (объект — персона), «Ольга Крячко (JIRA) works_at Artem Belov»,
«Olga Kryachko / Sintegrum works_at Olga Kryachko» (сама с собой). Замер
типов на проде 2026-09-13 (всего 4406 связей): works_at → supergroup 1174
(«IT outsource» 394, «Українці у Вʼєтнамі» 295 — участие в чате, а не
работа; оно и так есть в `memberships`), works_at → person 472. Типы
сущностей в графе: person, organization, bot, group, supergroup, channel —
отдельного типа «место» нет.
"""
from __future__ import annotations

import re

from vera_shared.graph.identity import canonical_name_parts
from vera_shared.graph.rel_text import (
    Evidence,
    claims_to_be_author,
    fact_names_both_ends,
    single_token_name,
)

# Слова, которые НИКОГДА не обозначают конкретного человека — даже если в
# графе есть аккаунт ровно с таким именем профиля.
#
# Инцидент 2026-08-20: у телеграм-аккаунта 942121006 имя профиля буквально
# «он». LLM исправно доставала факты из фраз вроде «Нормально он написал»,
# `resolve_entity_exact("он")` находила ровно одно совпадение — и факт
# прилипал к постороннему человеку. Так набралось 55 связей вида
# «Ли — coworker_of — он», «You never walk alone — reports_to — он».
# Местоимение неразрешимо без кореференции, поэтому такие связи не строим
# вовсе: пропустить факт дешевле, чем приписать его случайному человеку.
_NON_REFERENTIAL = {
    "он", "она", "оно", "они", "ты", "вы", "мы", "его", "её", "ее", "их",
    "им", "ему", "ей", "них", "нас", "вас", "этот", "эта", "тот", "та",
    "кто", "что", "все", "всё", "кое-кто", "некто",
    "he", "she", "it", "they", "them", "him", "her", "you", "we", "us",
    "this", "that", "who", "someone", "somebody", "everyone",
}

REJECT_SELF = "self_loop"
REJECT_LOW_CONFIDENCE = "low_confidence"
REJECT_NOT_REFERENTIAL = "not_referential"
REJECT_SERVICE_ACCOUNT = "service_account"
REJECT_TYPE = "type_mismatch"
REJECT_WEAK_NAME = "weak_name"
REJECT_FACT = "fact_mismatch"
REJECT_SELF_REFERENCE = "self_reference"

MIN_CONFIDENCE = 0.5

PERSON = "person"
ORGANIZATION = "organization"
_CHAT_TYPES = frozenset({"bot", "group", "supergroup", "channel"})

_PERSON_PERSON = frozenset({
    "boss_of", "reports_to", "coworker_of", "spouse_of", "parent_of",
    "child_of", "friend_of",
})
_PERSON_ORG = frozenset({"works_at", "co_founder_of"})
_PARTY_PARTY = frozenset({"client_of", "vendor_of"})
_PARTY = frozenset({PERSON, ORGANIZATION})

# «Ольга Крячко (JIRA)», «OpenRouter Team», «jira-bot» — в имени сказано, что
# это система или рассылка, даже если сущность заведена как person. Один
# список на запись связи и на гейт rel_policy.
TOOL_TAG_RE = re.compile(
    r"\(\s*(jira|confluence|github|gitlab|trello|slack|google calendar|"
    r"календарь|calendar)\s*\)", re.IGNORECASE)
_SERVICE_WORDS = frozenset({
    "team", "jira", "noreply", "no-reply", "bot", "notifications",
    "notification", "support", "newsletter", "mailer", "команда",
})
_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)


def is_referential_name(name: str | None) -> bool:
    """Может ли строка вообще обозначать конкретного человека.

    Отсекает местоимения и огрызки (одна буква, только знаки препинания) ДО
    похода в базу — иначе они резолвятся в случайный аккаунт с таким же
    именем профиля.
    """
    if not name:
        return False
    low = name.strip().lower()
    if len(low) < 2:
        return False
    if low in _NON_REFERENTIAL:
        return False
    return any(ch.isalnum() for ch in low)


def normalized_name(name: str | None) -> str:
    """Имя без приписок: «X / Org» → «X», «X (JIRA)» → «X», регистр и пробелы."""
    base = (name or "").split(" / ", 1)[0]
    base = re.sub(r"\([^)]*\)", " ", base)
    return " ".join(base.lower().split())


def same_person_name(a: str | None, b: str | None) -> bool:
    """Одно ли это имя с точностью до приписок, транслита и уменьшительных."""
    na, nb = normalized_name(a), normalized_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return canonical_name_parts(na) == canonical_name_parts(nb)


def is_service_name(name: str | None) -> bool:
    """Имя сущности выдаёт системный аккаунт, бота или рассылку."""
    if not name:
        return False
    if TOOL_TAG_RE.search(name):
        return True
    tokens = [t.lower() for t in _TOKEN_RE.findall(name)]
    # «_bot» только с разделителем: голое «…bot» ловит фамилии вроде Talbot.
    return any(t in _SERVICE_WORDS or t.endswith(("_bot", "-bot")) for t in tokens)


def _type_reject(predicate: str, subject_type: str | None,
                 object_type: str | None) -> bool:
    if subject_type in _CHAT_TYPES or object_type in _CHAT_TYPES:
        return True
    if predicate in _PERSON_PERSON:
        return not (subject_type == PERSON and object_type == PERSON)
    if predicate in _PERSON_ORG:
        return not (subject_type == PERSON and object_type == ORGANIZATION)
    if predicate in _PARTY_PARTY:
        return not (subject_type in _PARTY and object_type in _PARTY)
    if predicate == "lives_in":
        # Типа «место» нет: город резолвится во что попало. Человек «живёт в»
        # другом человеке — точно мусор (39 таких рёбер на 2026-09-13).
        return subject_type != PERSON or object_type == PERSON
    return True


def _weak_name(name: str | None, entity_type: str | None, strong: bool) -> bool:
    """Персона, названная одним словом («Андрей»), без сильного идентификатора:
    по такому имени в графе не отличить людей, и связь липнет к случайному."""
    return entity_type == PERSON and not strong and single_token_name(name)


def relationship_reject_reason(
    *, subject_name: str | None, subject_type: str | None, predicate: str,
    object_name: str | None, object_type: str | None, confidence: float,
    evidence: Evidence | None = None,
) -> str | None:
    """Почему связь нельзя писать в граф. None — можно.

    `evidence` (факт и сведения о концах) включает проверки «одно слово без
    сильного идентификатора» и «факт называет оба конца»; без него действуют
    только проверки по именам и типам.
    """
    if confidence < MIN_CONFIDENCE:
        return REJECT_LOW_CONFIDENCE
    if not (is_referential_name(subject_name) and is_referential_name(object_name)):
        return REJECT_NOT_REFERENTIAL
    if same_person_name(subject_name, object_name):
        return REJECT_SELF
    if _type_reject(predicate, subject_type, object_type):
        return REJECT_TYPE
    if is_service_name(subject_name) or is_service_name(object_name):
        return REJECT_SERVICE_ACCOUNT
    if evidence is None:
        return None
    if any(not end.author and claims_to_be_author(evidence.fact, end)
           for end in (evidence.subject, evidence.object)):
        return REJECT_SELF_REFERENCE
    if (_weak_name(subject_name, subject_type, evidence.subject.strong)
            or _weak_name(object_name, object_type, evidence.object.strong)):
        return REJECT_WEAK_NAME
    if not fact_names_both_ends(evidence):
        return REJECT_FACT
    return None
