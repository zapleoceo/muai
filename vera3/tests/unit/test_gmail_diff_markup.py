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


R = '<span class="diff-html-removed">'


def _old(html: str) -> str:
    import re
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def test_ordinary_html_is_byte_identical_to_old_path():
    for html in ("<p>a [ 1 ] b ]</p>", "<p>Привет, <b>Дима</b>!</p>",
                 "<div>x ]  y</div><style>.a{}</style>", "a &amp; [ b ]"):
        assert html_to_text(html) == _old(html)


def test_brackets_in_diff_email_text_are_kept():
    out = html_to_text(f"<p>a [ 1 ] b ] {R}x</span></p>")
    assert out == "a [ 1 ] b ] [удалено: x]"


def test_newsletter_strikethrough_is_not_a_diff():
    for html in ("<s>$50</s> $30", "<del>$50</del> $30", "<strike>$50</strike> $30",
                 '<span class="line-through">$50</span> $30',
                 '<span style="text-decoration: line-through">$50</span> $30'):
        assert not has_diff_markup(html)
        assert html_to_text(html) == _old(html)


def test_stray_end_tag_does_not_lose_closing_marker():
    assert html_to_text(f"{R}a</b>b</span> c") == "[удалено: ab] c"


def test_unclosed_marker_is_closed_at_end():
    assert html_to_text(f"x {R}a <b>b") == "x [удалено: a b]"


def test_nested_markers_are_not_doubled():
    out = html_to_text(f"{R}a {R}b</span> c</span> d")
    assert out.count("[удалено:") == 1 and out.count("]") == 1


def test_oversize_falls_back_and_pathological_input_is_fast():
    import time
    big = R + "x" * (512 * 1024) + "</span>"
    assert not has_diff_markup(big)
    t = time.monotonic()
    html_to_text(big)
    html_to_text("<p>" * 150000 + R + "x</span>")
    assert time.monotonic() - t < 5



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
