"""Кнопки владельца на странице офиса: только штатные цвета темы, читаемый контраст."""
from __future__ import annotations

import re
from types import SimpleNamespace

from dashboard.office_owner_actions_view import ACTIONS_CSS, owner_actions_bar
from dashboard.ui.css_base import TOKENS_CSS


def _bar(status: str = "blocked") -> str:
    row = SimpleNamespace(room="main", task_id="T-1", status=status)
    return owner_actions_bar(SimpleNamespace(row=row))


def test_buttons_use_theme_classes_and_cancel_is_danger():
    html = _bar()
    classes = re.findall(r'<button type="submit" class="([^"]+)">([^<]+)</button>', html)
    assert ("sm secondary", "Продолжить") in classes
    assert ("sm danger", "Отменить") in classes
    assert all(cls.startswith("sm ") for cls, _ in classes)


def test_action_css_sets_no_own_button_colors():
    assert "#fff" not in ACTIONS_CSS and "--panel" not in ACTIONS_CSS
    assert ".kb-act button" not in ACTIONS_CSS


def _lum(hex_color: str) -> float:
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a: str, b: str) -> float:
    x, y = _lum(a), _lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def test_faint_and_muted_text_meet_wcag_aa_on_every_theme_background():
    token = dict(re.findall(r"--([a-z0-9-]+):(#[0-9a-f]{6})", TOKENS_CSS))
    backgrounds = [token[k] for k in ("bg", "bg-raised", "bg-inset", "surface",
                                      "surface-2", "surface-3")]
    for name in ("faint", "muted", "text"):
        worst = min(_contrast(token[name], bg) for bg in backgrounds)
        assert worst >= 4.5, f"--{name} contrast {worst:.2f} < 4.5"
