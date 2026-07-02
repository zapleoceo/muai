"""Тесты правил принадлежности к проектам (папки TG + имена)."""
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
