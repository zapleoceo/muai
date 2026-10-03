"""Детектор точных дублей графа — пять случаев аудита 2026-09-26.

Правила осознанно узкие: лучше пропустить дубль, чем слить двух разных людей.
Всё, что не прошло проверку однозначности, уходит в `skipped` с причиной, а не
молча теряется — владелец видит это в плане.

1. человек из Telegram и тот же человек из рабочей почты/Slack (имя совпало с
   точностью до транслита, и никого третьего с таким именем нет);
2. организации, заведённые по адресу отправителя, а не по домену;
3. группа, ставшая супергруппой (две сущности с одним именем);
4. заглушка `tg_user_<id>` на месте чата с тем же tg_id;
5. сервисные отправители, записанные как person.

НЕ сливаются: тёзки с разными Telegram-аккаунтами; канал и его отдельная
группа обсуждений.
"""
from __future__ import annotations

from collections import defaultdict

from vera_shared.graph.dupe_actions import (
    Action,
    heaviest,
    merge_action,
    retype_action,
    skip_action,
)
from vera_shared.graph.dupe_keys import name_key, org_domain, plain_name
from vera_shared.graph.dupe_orgs import detect_generic_names, detect_orgs
from vera_shared.graph.dupe_snapshot import Ent, Snapshot
from vera_shared.graph.identity import entity_kind_for_email

CHAT_TYPES = ("group", "supergroup", "channel")
WORK_DOMAIN = "itstep.org"
PLACEHOLDER = "tg_user_"


# ─── 1. человек: Telegram ↔ рабочая почта/Slack ─────────────────────────────


def _is_telegram_person(e: Ent) -> bool:
    return e.tg_id is not None or any(i.startswith("user:") for i in e.identifiers("telegram"))


def _is_work_person(e: Ent) -> bool:
    if _is_telegram_person(e):
        return False
    mails = [m.lower() for m in e.identifiers("gmail")]
    return any(m.endswith("@" + WORK_DOMAIN) for m in mails) or any(
        i.startswith("user:") for i in e.identifiers("slack"))


def detect_people(snap: Snapshot) -> list[Action]:
    by_key: dict[str, list[Ent]] = defaultdict(list)
    for e in snap.of_type("person"):
        if key := name_key(e.name):
            by_key[key].append(e)
    out: list[Action] = []
    for group in (by_key[k] for k in sorted(by_key)):
        tg = [e for e in group if _is_telegram_person(e)]
        work = [e for e in group if _is_work_person(e)]
        if not tg or not work:
            continue
        if len(group) != 2 or len(tg) != 1 or len(work) != 1:
            out.append(skip_action(1, group, "неоднозначно: у имени есть другие кандидаты "
                                       "(тёзки или несколько рабочих записей)"))
            continue
        keep = heaviest(group)
        drop = [e for e in group if e is not keep]
        out.append(merge_action(1, keep, drop, "то же полное имя (с точностью до транслита): "
                          "Telegram-аккаунт + рабочая почта/Slack, третьих нет"))
    return out


# ─── 3. группа → супергруппа ────────────────────────────────────────────────


def _migration_proof(group: Ent, supergroup: Ent) -> bool:
    """Telegram-доказательство миграции: взаимные ссылки в атрибутах или алиас
    старого id у супергруппы (его дописывает `resolve_migrated_chat`)."""
    attrs_old, attrs_new = group.attributes, supergroup.attributes
    if group.tg_id is None:
        return False
    return (str(attrs_new.get("migrated_from")) == group.tg_id
            or (supergroup.tg_id is not None
                and str(attrs_old.get("migrated_to")) == supergroup.tg_id)
            or f"chat:{group.tg_id}" in supergroup.identifiers("telegram"))


def detect_migrated_chats(snap: Snapshot) -> list[Action]:
    by_name: dict[str, list[Ent]] = defaultdict(list)
    for e in snap.of_type(*CHAT_TYPES):
        if key := plain_name(e.name):
            by_name[key].append(e)
    out: list[Action] = []
    for group in by_name.values():
        groups = [e for e in group if e.type == "group"]
        supers = [e for e in group if e.type == "supergroup"]
        if len(group) < 2 or not groups or not supers:
            continue
        if len(group) != 2 or any(e.type == "channel" for e in group):
            out.append(skip_action(3, group, "неоднозначно: больше двух чатов с этим именем "
                                             "или среди них канал"))
        elif not _migration_proof(groups[0], supers[0]):
            out.append(skip_action(3, group, "нет доказательства миграции: одно имя "
                                             "и типы group + supergroup — мало, это могут "
                                             "быть два разных чата"))
        else:
            out.append(merge_action(3, supers[0], groups, "группа переехала в супергруппу: "
                                    "подтверждено migrated_from/migrated_to или алиасом "
                                    "старого id"))
    return out


# ─── 4. заглушка tg_user_<id> вместо чата ───────────────────────────────────


def detect_placeholders(snap: Snapshot) -> list[Action]:
    chats: dict[str, list[Ent]] = defaultdict(list)
    for e in snap.of_type(*CHAT_TYPES):
        if e.tg_id:
            chats[e.tg_id].append(e)
    out: list[Action] = []
    for e in snap.of_type("person"):
        if not e.name.startswith(PLACEHOLDER) or e.tg_id is None:
            continue
        if e.name != f"{PLACEHOLDER}{e.tg_id}" or e.attributes.get("username"):
            continue
        match = chats.get(e.tg_id, [])
        if match and e.tg_id not in snap.self_posting:
            out.append(skip_action(4, [e, *match], "нет доказательства: в событиях нет "
                                                   "сообщения с sender_id == chat_id — "
                                                   "совпадение id может быть случайным"))
        elif len(match) == 1:
            out.append(merge_action(4, match[0], [e], "заглушка tg_user_<id> совпала по tg_id "
                              "с чатом — отправитель и есть этот чат"))
        elif len(match) > 1:
            out.append(skip_action(4, [e, *match], "tg_id совпал с несколькими чатами"))
    return out


# ─── 5. сервисные отправители, записанные как person ────────────────────────


def _only_mail(e: Ent) -> bool:
    return bool(e.identifiers("gmail")) and all(s == "gmail" for s, _ in e.aliases) \
        and e.tg_id is None


def _is_service_sender(e: Ent) -> bool:
    for mail in e.identifiers("gmail"):
        domain = org_domain(mail)
        if domain is None:
            return False
        if entity_kind_for_email(mail) == "organization":
            return True
        label = "".join(ch for ch in e.name.casefold() if ch.isalnum())
        if label and label in (domain.replace(".", ""), domain.split(".")[0]):
            return True
    return False


def detect_service_people(snap: Snapshot) -> list[Action]:
    return [retype_action(5, e, "organization",
                          "сервисный отправитель (служебный адрес или имя = бренд домена)")
            for e in snap.of_type("person") if _only_mail(e) and _is_service_sender(e)]


# ─── всё вместе ─────────────────────────────────────────────────────────────


def build_plan(snap: Snapshot) -> list[Action]:
    """Порядок важен при применении: retype → переименования → слияния."""
    retypes = detect_service_people(snap)
    becoming = {a["entity"]: snap.entities[a["entity"]] for a in retypes}
    orgs = detect_orgs(snap, becoming)
    renamed = {a["entity"] for a in orgs if a["action"] == "rename"}
    merged = {i for a in orgs if a["action"] == "merge" for i in a["drop"]}
    retypes = [a for a in retypes if a["entity"] not in merged]
    generic = detect_generic_names(snap, renamed | merged)
    # заглушка вливается в группу раньше, чем сама группа — в супергруппу
    merges = [*detect_people(snap), *detect_placeholders(snap), *detect_migrated_chats(snap)]
    ordered = [*retypes, *[a for a in (*orgs, *generic) if a["action"] == "rename"],
               *[a for a in orgs if a["action"] == "merge"], *merges,
               *[a for a in orgs if a["action"] == "skip"]]
    for n, action in enumerate(ordered, 1):
        action["id"] = n
    return ordered
