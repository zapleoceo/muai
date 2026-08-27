"""Проект для Slack — правило владельца, а не догадка модели.

Вся переписка в рабочем пространстве относится к itstep. Модель на коротких
репликах систематически врала: «)))» и «згоден» уезжали в family и personal —
174 события из 815 на 2026-08-27.

Правило живёт данными в `project_membership` (kind='account'), а код обязан
применять его к ЛЮБОМУ источнику: значения `events.account` по источникам не
пересекаются, поэтому шаблон бьёт ровно туда, куда заведён.
"""
from __future__ import annotations

import inspect
import re

from brain_triage import project_override


def _account_statement() -> str:
    """Текст SQL-выражения, которое применяет membership по аккаунту."""
    source = inspect.getsource(project_override.apply_project_override)
    blocks = re.findall(r'text\("""(.*?)"""\)', source, re.S)
    matching = [b for b in blocks if "pm.kind='account'" in b]
    assert len(matching) == 1, f"ожидал одно правило по аккаунту, нашёл {len(matching)}"
    return matching[0]


class TestAccountRule:
    def test_rule_is_not_limited_to_one_source(self):
        """С фильтром по gmail правило не доставало ни Slack, ни будущих источников."""
        statement = _account_statement()
        assert "e.account ILIKE pm.key" in statement
        assert "e.source=" not in statement.replace(" ", ""), (
            "правило по аккаунту не должно быть привязано к источнику")

    def test_rule_only_writes_when_project_differs(self):
        """Иначе каждый батч переписывал бы одни и те же строки."""
        assert "IS DISTINCT FROM pm.project" in _account_statement()

    def test_rule_stays_scoped_to_the_batch(self):
        assert "e.id = ANY(:ids)" in _account_statement()
