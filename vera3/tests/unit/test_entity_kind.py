"""identity.entity_kind_for_email — служебный ящик ≠ человек.

Аудит 2026-08-06: 145 «персон» в графе были сервисными адресами
(no-reply@alerts.airasia.com, invoice+statements@mail.anthropic.com,
crm@itstep.org), 134 из них без единой связи. Одна организация с четырёх
адресов выглядела как четыре одноимённых дубля."""
from __future__ import annotations

import pytest
from vera_shared.graph.identity import entity_kind_for_email


@pytest.mark.parametrize("addr", [
    "no-reply@alerts.airasia.com",
    "noreply@business.facebook.com",
    "invoice+statements@mail.anthropic.com",     # плюс-адресация
    "failed-payments@mail.anthropic.com",        # сервисный поддомен
    "welcome@cerebras.net",
    "info@cerebras.net",
    "crm_kiev@itstep.org",                       # подчёркивание как разделитель
    "crm@itstep.org",
    "logbook@itstep.org",
    "ads-account-noreply@google.com",            # служебное слово НЕ в начале
])
def test_service_addresses_are_organizations(addr):
    assert entity_kind_for_email(addr) == "organization"


@pytest.mark.parametrize("addr", [
    "zaporozec_d@itstep.org",
    "demoniwwwe@gmail.com",
    "kravchenko_k@itstep.org",
    "risma@permitindo.com",
    "ericsimons@bolt.new",
    "yegorov@itstep.org",
])
def test_real_people_stay_person(addr):
    assert entity_kind_for_email(addr) == "person"


@pytest.mark.parametrize("addr", [None, "", "нет-собаки", "@только-домен.com"])
def test_garbage_defaults_to_person(addr):
    # не угадали — пусть будет человек: занизить тип безопаснее, чем
    # молча выкинуть реального собеседника из графа людей
    assert entity_kind_for_email(addr) == "person"


def test_word_boundary_not_substring():
    # «information@» — не «info@»; «supportive@» — не «support@»
    assert entity_kind_for_email("informationsecurity@corp.com") == "person"
    assert entity_kind_for_email("supportive-care@clinic.com") == "person"
    assert entity_kind_for_email("info-eu@corp.com") == "organization"
