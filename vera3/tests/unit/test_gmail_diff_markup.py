"""Jira diff-разметка в письме: удалённое помечается, не сливается с новым."""
from __future__ import annotations

import base64
import os
import sys

_SERVICES = os.path.join(os.path.dirname(__file__), "..", "..", "services")
for _svc in ("ingestor-gmail", "brain-search", "brain-triage"):
    sys.path.insert(0, os.path.join(_SERVICES, _svc, "src"))

os.environ.setdefault("GMAIL_CLIENT_ID", "test-cid")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "test-csec")

from ingestor_gmail.html_text import has_diff_markup, html_to_text  # noqa: E402
from ingestor_gmail.poller import _extract_text  # noqa: E402

JIRA = (
    '<p>ADR <span class="diff-html-removed">0014 платформа выплачивает</span>'
    '<span class="diff-html-added">0022 студент платит школе</span> готово</p>'
)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def test_removed_and_added_are_marked():
    out = html_to_text(JIRA)
    assert "[удалено: 0014 платформа выплачивает]" in out
    assert "[добавлено: 0022 студент платит школе]" in out


def test_del_s_strike_and_linethrough_marked_as_removed():
    for html in ("<del>x</del>", "<s>x</s>", "<strike>x</strike>",
                 '<span style="text-decoration: line-through">x</span>'):
        assert html_to_text(f"a {html} b") == "a [удалено: x] b"


def test_nested_tags_inside_removed_close_correctly():
    out = html_to_text("<del><b>старое</b> требование</del> новое")
    assert out == "[удалено: старое требование] новое"


def test_plain_html_unchanged():
    assert html_to_text("<p>Привет, <b>Дима</b>!</p>") == "Привет, Дима !"
    assert not has_diff_markup("<p>текст</p>")


def test_multipart_prefers_html_with_diff_over_plain():
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("0014 0022 слито")}},
        {"mimeType": "text/html", "body": {"data": _b64(JIRA)}},
    ]}
    out = _extract_text(payload)
    assert "[удалено: 0014" in out and "слито" not in out


def test_multipart_without_diff_keeps_plain():
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("plain")}},
        {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
    ]}
    assert _extract_text(payload) == "plain"


def test_synthesis_and_triage_rules_cover_removed_text_and_ready_status():
    from brain_search.authorship import AUTHORSHIP_RULES
    from brain_triage.prompts import TRIAGE_PROMPT_TEMPLATE

    assert "[удалено: …]" in AUTHORSHIP_RULES
    assert "Ready to Prod" in AUTHORSHIP_RULES
    assert "готовность, а не выкатку" in AUTHORSHIP_RULES
    assert "[удалено: …]" in TRIAGE_PROMPT_TEMPLATE
    assert "Ready to Prod" in TRIAGE_PROMPT_TEMPLATE
