"""Прозвище можно сузить проектом: инициалы директора («ДА») должны ловиться в
чатах IT STEP, но не в чатах другого бизнеса владельца (04.10.2026: отчёт по
области показал рабочий чат Веранды среди засчитанных)."""
from __future__ import annotations

from pathlib import Path

import pytest
from vera_shared.links.scope import (
    CHATS,
    CONTACTS,
    GLOBAL,
    PROJECT_PREFIX,
    WORK,
    ChatContext,
    NicknameRule,
    ScopeError,
    in_scope,
    scope_ids_for,
)


def test_project_becomes_a_scope_id_next_to_chats():
    assert scope_ids_for(WORK, ["telegram:1"], "itstep") == ["telegram:1", PROJECT_PREFIX + "itstep"]
    assert scope_ids_for(CONTACTS, None, "itstep") == [PROJECT_PREFIX + "itstep"]


def test_no_project_keeps_chats_as_is():
    assert scope_ids_for(CHATS, ["telegram:1"], None) == ["telegram:1"]
    assert scope_ids_for(GLOBAL, None, None) == []


@pytest.mark.parametrize("kind", [CHATS, GLOBAL])
def test_project_is_refused_where_it_would_silently_do_nothing(kind):
    with pytest.raises(ScopeError):
        scope_ids_for(kind, None, "itstep")


@pytest.mark.parametrize("bad", ["", "telegram:1", "project:itstep", "IT STEP", "x" * 41])
def test_project_must_be_a_plain_slug(bad):
    with pytest.raises(ScopeError):
        scope_ids_for(WORK, None, bad)


def test_project_narrowed_rule_skips_other_business_work_chat():
    rule = NicknameRule(1, "ДА", True, WORK, (PROJECT_PREFIX + "itstep",))
    itstep = ChatContext(chat_key="telegram:10", is_work=True, project="itstep")
    veranda = ChatContext(chat_key="telegram:20", is_work=True, project="veranda")
    assert in_scope(rule, itstep, frozenset())
    assert not in_scope(rule, veranda, frozenset())


def test_cli_and_mcp_build_scope_ids_the_same_way():
    import inspect

    from vera_mcp import link_write_tools

    assert "scope_ids_for(scope, chats, project)" in inspect.getsource(link_write_tools.entity_add_nickname)
    script = (Path(__file__).resolve().parents[2] / "scripts" / "manage_nicknames.py").read_text(encoding="utf-8")
    assert script.count("scope_ids_for(args.scope, args.chat, args.project)") == 2
    assert '"--project"' in script and "ScopeError" in script


def test_chats_scope_without_chats_is_refused():
    with pytest.raises(ScopeError):
        scope_ids_for(CHATS, [], None)
