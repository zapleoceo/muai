"""Тесты правил принадлежности к проектам (папки TG + имена)."""
# ruff: noqa: I001  # env setup intentionally split around imports
from __future__ import annotations

import os

os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")

from vera_shared.projects.rules import (  # noqa: E402
    OWNER_TG_ID, chat_key, folder_to_project, is_owner, match_name,
)


class TestFolder:
    def test_itstep_folder(self):
        assert folder_to_project("ItStep") == "itstep"

    def test_itstep_case_space_dash_insensitive(self):
        assert folder_to_project("IT Step") == "itstep"
        assert folder_to_project("it-step") == "itstep"
        assert folder_to_project("  ITSTEP ") == "itstep"

    def test_unknown_folder_none(self):
        assert folder_to_project("Work") is None
        assert folder_to_project("Проекты") is None

    def test_none_input(self):
        assert folder_to_project(None) is None
        assert folder_to_project("") is None


class TestNameRule:
    def test_veranda_latin(self):
        assert match_name("Veranda менеджмент") == "veranda"
        assert match_name("GameZone & Veranda") == "veranda"

    def test_veranda_cyrillic(self):
        assert match_name("Веранда сотрудники") == "veranda"

    def test_veranda_case_insensitive(self):
        assert match_name("VERANDA transactions") == "veranda"

    def test_non_project_chat(self):
        assert match_name("J Branch Internal") is None
        assert match_name("NEXTA Live") is None

    def test_none(self):
        assert match_name(None) is None


class TestChatKey:
    def test_abs_normalizes_sign(self):
        assert chat_key(-5259663113) == 5259663113
        assert chat_key(5259663113) == 5259663113

    def test_strips_supergroup_100_prefix(self):
        # Обе формы одного супергруппового чата → один ключ
        assert chat_key(3889942420) == 3889942420
        assert chat_key(1003889942420) == 3889942420
        assert chat_key(-1003889942420) == 3889942420

    def test_string_input(self):
        assert chat_key("-1003889942420") == 3889942420


class TestOwner:
    def test_owner_detected(self):
        assert is_owner(169510539) is True
        assert is_owner("169510539") is True

    def test_non_owner(self):
        assert is_owner(182336337) is False

    def test_none(self):
        assert is_owner(None) is False

    def test_owner_id_constant(self):
        assert OWNER_TG_ID == 169510539


class TestSharedVocabulary:
    """Словарь проектов один: триаж и голосовой маршрут берут его отсюда."""

    def test_project_key_is_case_insensitive(self):
        from vera_shared.projects.rules import project_key
        assert project_key("Veranda") == "veranda"
        assert project_key(" ITSTEP ") == "itstep"
        assert project_key("Vera") is None
        assert project_key(None) is None

    def test_match_context_app_rules_before_names(self):
        from vera_shared.projects.rules import match_context
        assert match_context("chrome.exe SIN-4792 - Jira") == "itstep"
        assert match_context("Telegram.exe Веранда - финансы") == "veranda"
        assert match_context("jira veranda") == "itstep"
        assert match_context(None) is None

    def test_rules_only_name_valid_projects(self):
        from vera_shared.projects.rules import (
            ACCOUNT_RULES, APP_RULES, FOLDER_TO_PROJECT, NAME_RULES,
            PROJECTS, QUERY_TRIGGERS, VALID_PROJECTS,
        )
        assert set(PROJECTS) == VALID_PROJECTS
        named = (set(FOLDER_TO_PROJECT.values()) | set(NAME_RULES)
                 | set(ACCOUNT_RULES) | set(QUERY_TRIGGERS) | set(APP_RULES))
        assert named <= VALID_PROJECTS

    def test_voice_route_uses_shared_vocabulary(self):
        from vera_shared.projects.rules import VALID_PROJECTS
        from vera_shared.voice_help.policy import DEFAULT_PROJECT, project_for
        assert project_for("Telegram.exe", "Veranda chat") in VALID_PROJECTS
        assert project_for("Code.exe", "myAI") == DEFAULT_PROJECT
