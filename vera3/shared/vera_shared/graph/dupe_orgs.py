"""Случай 2: организации заведены по адресу отправителя, а не по домену.

Одна компания с десятка адресов (`no-reply@`, `billing@`, `news@`…) — это десяток
организаций. Сливаем по зарегистрированному домену, но не вслепую: имя должно
быть похоже на бренд домена, а платформы рассылок (stripe.com, zendesk.com)
не группируются вовсе — под ними живут тысячи чужих компаний.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from vera_shared.graph.dupe_actions import (
    Action,
    merge_action,
    rename_action,
    skip_action,
)
from vera_shared.graph.dupe_keys import (
    SHARED_PLATFORMS,
    alnum,
    brand_fit,
    domain_brand,
    is_generic_name,
    org_domain,
    registrable_domain,
)
from vera_shared.graph.dupe_snapshot import Ent, Snapshot


def _domains(e: Ent) -> set[str]:
    found = {org_domain(m) for m in e.identifiers("gmail")}
    found |= {registrable_domain(d) for d in e.identifiers("domain")}
    found.add(org_domain(e.attributes.get("email")))
    return {d for d in found if d}


def _mail(e: Ent) -> str | None:
    return (e.identifiers("gmail") or [None])[0]


def _pick_keeper(domain: str, group: list[Ent]) -> Ent:
    anchored = [e for e in group if domain in e.identifiers("domain")]
    if anchored:
        return anchored[0]
    return min(group, key=lambda e: (brand_fit(e.name, domain, _mail(e)) != "exact",
                                     -e.degree, e.id))


def _best_name(group: list[Ent], keep: Ent, domain: str) -> str | None:
    names = Counter(e.name for e in group
                    if brand_fit(e.name, domain, _mail(e)) in ("exact", "fits"))
    top = names.most_common(1)
    return top[0][0] if top and top[0][0] != keep.name else None


def _foreign_actions(domain: str, foreign: list[Ent]) -> list[Action]:
    """Имена, не похожие на бренд: сливаем только буквально одинаковые (один
    домен + одно имя), остальное оставляем владельцу."""
    by_name: dict[str, list[Ent]] = defaultdict(list)
    for e in foreign:
        by_name[alnum(e.name)].append(e)
    out: list[Action] = []
    for twins in by_name.values():
        if len(twins) > 1:
            keep = _pick_keeper(domain, twins)
            out.append(merge_action(2, keep, [e for e in twins if e is not keep],
                              f"один домен {domain} и одно и то же имя"))
        else:
            out.append(skip_action(2, twins, f"имя не похоже на бренд {domain} (живой человек "
                                       "в заголовке рассылки или другой бренд) — не трогаю"))
    return out


def detect_orgs(snap: Snapshot, extra: dict[int, Ent] | None = None) -> list[Action]:
    """`extra` — сущности, которые станут организациями после retype (случай 5)."""
    pool = [*snap.of_type("organization"), *(extra or {}).values()]
    by_domain: dict[str, list[Ent]] = defaultdict(list)
    out: list[Action] = []
    for e in pool:
        doms = _domains(e)
        if len(doms) > 1:
            out.append(skip_action(2, [e], f"адреса с разных доменов {sorted(doms)} — "
                                     "склеено вручную, разделить автоматически нельзя"))
        elif doms:
            by_domain[next(iter(doms))].append(e)
    for domain, members in sorted(by_domain.items()):
        if len(members) < 2:
            continue
        if domain in SHARED_PLATFORMS:
            out.append(skip_action(2, members, f"{domain} — платформа рассылок, под одним "
                                          "доменом разные компании"))
            continue
        foreign = [e for e in members if brand_fit(e.name, domain, _mail(e)) == "foreign"]
        group = [e for e in members if e not in foreign]
        out.extend(_foreign_actions(domain, foreign))
        if len(group) < 2:
            continue
        keep = _pick_keeper(domain, group)
        anchored = domain in keep.identifiers("domain")
        how = "есть сущность domain:" if anchored else "имя совпало с брендом / больше связей"
        out.append(merge_action(2, keep, [e for e in group if e is not keep],
                          f"один домен {domain}, keep — {how}"))
        better = _best_name(group, keep, domain)
        if better and brand_fit(keep.name, domain, _mail(keep)) == "generic":
            out.append(rename_action(2, keep, better, f"имя из local-part → имя из группы {domain}"))
    return out


def detect_generic_names(snap: Snapshot, handled: set[int]) -> list[Action]:
    """Организации под именем «noreply»/«info»: имя взято из local-part."""
    out: list[Action] = []
    for e in snap.of_type("organization"):
        mail = _mail(e)
        domain = org_domain(mail)
        if e.id in handled or not domain or not is_generic_name(e.name, mail):
            continue
        out.append(rename_action(2, e, domain_brand(domain),
                           f"имя «{e.name}» взято из адреса — подставлен домен {domain}"))
    return out
