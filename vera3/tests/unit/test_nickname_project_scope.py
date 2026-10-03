"""Прозвище можно сузить проектом: инициалы директора («ДА») должны ловиться в
чатах IT STEP, но не в чатах другого бизнеса владельца (04.10.2026: отчёт по
области показал рабочий чат Веранды среди засчитанных)."""
from __future__ import annotations

from vera_shared.links.scope import PROJECT_PREFIX, scope_ids_for


def test_project_becomes_a_scope_id_next_to_chats():
    assert scope_ids_for(["telegram:1"], "itstep") == ["telegram:1", PROJECT_PREFIX + "itstep"]


def test_no_project_keeps_chats_as_is():
    assert scope_ids_for(["telegram:1"], None) == ["telegram:1"]
    assert scope_ids_for(None, None) == []


def test_cli_and_mcp_build_scope_ids_the_same_way():
    import inspect

    from vera_mcp import link_write_tools

    assert "scope_ids_for(chats, project)" in inspect.getsource(link_write_tools.entity_add_nickname)
    script = (__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"
              / "manage_nicknames.py").read_text(encoding="utf-8")
    assert script.count("scope_ids_for(args.chat, args.project)") == 2
    assert '"--project"' in script
