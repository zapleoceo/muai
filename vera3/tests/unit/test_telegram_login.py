"""dashboard.telegram_login — pure form/escaping helpers render safely."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..", "services", "dashboard", "src"))

from dashboard.telegram_login import (  # noqa: E402
    _code_form,
    _esc,
    _page,
    _password_form,
    _phone_form,
)


def test_esc_neutralizes_html():
    assert _esc('<b>&"x"') == "&lt;b&gt;&amp;&quot;x&quot;"


def test_phone_form_prefills_env_phone(monkeypatch):
    monkeypatch.setenv("TELEGRAM_PHONE", "+380994811889")
    html = _phone_form()
    assert "+380994811889" in html
    assert 'action="/api/telegram/start"' in html


def test_code_form_carries_flow_id_and_posts_to_verify():
    html = _code_form("abc123")
    assert 'value="abc123"' in html
    assert 'action="/api/telegram/verify"' in html


def test_password_form_uses_password_input():
    html = _password_form("f1")
    assert 'type="password"' in html
    assert 'value="f1"' in html


def test_page_sets_status_code():
    resp = _page("t", "<h1>x</h1>", code=400)
    assert resp.status_code == 400
