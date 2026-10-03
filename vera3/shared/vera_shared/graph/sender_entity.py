"""Отправитель письма → спецификация сущности для `sync_author_entities`.

Организация живёт на ДОМЕНЕ, а не на адресе: до 2026-09-26 каждый ящик
(`no-reply@`, `billing@`, `news@`) заводил свою организацию — 107 лишних, из них
Anthropic ×25 и Google ×19. Теперь у организации есть алиас `(domain, <домен>)`:
письмо с нового адреса цепляется к уже существующей через `known_as`.

Имя из local-part («noreply», «info») название компании не говорит: подставляем
бренд домена, иначе несвязанные компании встают под одним именем.
"""
from __future__ import annotations

from typing import Any

from vera_shared.graph.dupe_keys import SHARED_PLATFORMS, domain_brand, is_generic_name, org_domain
from vera_shared.graph.identity import entity_kind_for_email


def sender_entity(addr: str, display: str) -> dict[str, Any]:
    kind = entity_kind_for_email(addr)
    spec: dict[str, Any] = {
        "type": kind, "name": display, "identifier": addr,
        "display_name": display, "attributes": {"email": addr},
    }
    if kind != "organization":
        return spec
    domain = org_domain(addr)
    # На платформах рассылок под одним доменом живут разные компании — там
    # по-прежнему один адрес = одна организация.
    if domain is None or domain in SHARED_PLATFORMS:
        return spec
    if is_generic_name(display, addr):
        spec["name"] = domain_brand(domain)
    spec["known_as"] = [("domain", domain)]
    spec["attributes"] = {"email": addr, "domain": domain}
    return spec
