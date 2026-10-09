"""Jira diff-разметка в письме: удалённое помечается, не сливается с новым."""
from __future__ import annotations

import base64
import os
import re
import sys
import time

_SERVICES = os.path.join(os.path.dirname(__file__), "..", "..", "services")
for _svc in ("ingestor-gmail", "brain-search", "brain-triage"):
    sys.path.insert(0, os.path.join(_SERVICES, _svc, "src"))

os.environ.setdefault("GMAIL_CLIENT_ID", "test-cid")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "test-csec")

from ingestor_gmail.html_text import (  # noqa: E402
    has_diff_markup,
    html_to_text,
    is_jira_sender,
)
from ingestor_gmail.poller import _extract_text  # noqa: E402

JIRA_FROM = '"Igor Gladkyi (JIRA)" <jira@itstep.atlassian.net>'
R = '<span class="diff-removed">'
A = '<span class="diff-added">'

# Структура реального уведомления Jira, текст синтетический.
JIRA_BODY = (
    '<h2><span style="background-color:#ffebe6;text-decoration:line-through">'
    'Старый заголовок</span> <span style="background-color:#e3fcef">Новый заголовок'
    '</span></h2>'
    f'<p>Зачем: {R}платформа выплачивает</span> '
    f'<span class="diff-changed">{R}ADR 0014</span>{A}ADR 0022</span></span> '
    f'готово. {A}Добавленная строка</span></p>'
    '<p><span class="diff-changed">Одиночное изменение</span></p>'
)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def _old(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def test_real_jira_structure_is_marked():
    out = html_to_text(JIRA_BODY, jira=True)
    assert "[удалено: Старый заголовок]" in out
    assert "[добавлено: Новый заголовок]" in out
    assert "[удалено: платформа выплачивает]" in out
    assert "[удалено: ADR 0014] [добавлено: ADR 0022]" in out
    assert "[добавлено: Добавленная строка]" in out
    assert "[добавлено: Одиночное изменение]" in out
    assert out.count("[добавлено: ADR 0022]") == 1  # changed-обёртка не удваивает


def test_legacy_diff_html_classes_still_work():
    out = html_to_text('<span class="diff-html-removed">a</span>'
                       '<span class="diff-html-added">b</span>', jira=True)
    assert out == "[удалено: a] [добавлено: b]"


def test_non_jira_sender_with_diff_class_is_unchanged():
    html = f"<p>x {R}a</span> {A}b</span></p>"
    assert not is_jira_sender('"Shop" <news@shop.example>')
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("plain")}},
        {"mimeType": "text/html", "body": {"data": _b64(html)}},
    ]}
    assert _extract_text(payload, "m", '"Shop" <news@shop.example>') == "plain"
    assert html_to_text(html) == _old(html)


def test_jira_sender_detection():
    assert is_jira_sender(JIRA_FROM)
    assert is_jira_sender("jira@acme.atlassian.net")
    assert not is_jira_sender("evil@atlassian.net.example.com")
    assert not is_jira_sender("")


def test_newsletter_line_through_is_unchanged_even_for_jira_mode():
    for html in ("<s>$50</s> $30", "<del>$50</del> $30",
                 '<span class="line-through">$50</span> $30',
                 '<span style="text-decoration: line-through">$50</span> $30'):
        assert not has_diff_markup(html)
        assert html_to_text(html, jira=True) == _old(html)


def test_ordinary_html_is_byte_identical_to_old_path():
    for html in ("<p>a [ 1 ] b ]</p>", "<p>Привет, <b>Дима</b>!</p>",
                 "<div>x ]  y</div><style>.a{}</style>", "a &amp; [ b ]"):
        assert html_to_text(html) == _old(html)
        assert html_to_text(html, jira=True) == _old(html)


def test_brackets_in_diff_email_text_are_kept():
    out = html_to_text(f"<p>a [ 1 ] b ] {R}x</span></p>", jira=True)
    assert out == "a [ 1 ] b ] [удалено: x]"


def test_stray_end_tag_does_not_lose_closing_marker():
    assert html_to_text(f"{R}a</b>b</span> c", jira=True) == "[удалено: ab] c"


def test_unclosed_marker_is_closed_at_end():
    assert html_to_text(f"x {R}a <b>b", jira=True) == "x [удалено: a b]"


def test_nested_markers_are_not_doubled():
    out = html_to_text(f"{R}a {R}b</span> c</span> d", jira=True)
    assert out.count("[удалено:") == 1 and out.count("]") == 1


def test_multipart_jira_prefers_html_with_diff_over_plain():
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("0014 0022 слито")}},
        {"mimeType": "text/html", "body": {"data": _b64(JIRA_BODY)}},
    ]}
    out = _extract_text(payload, "m", JIRA_FROM)
    assert "[удалено: платформа выплачивает]" in out and "слито" not in out


def test_multipart_jira_without_diff_keeps_plain():
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("plain")}},
        {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
    ]}
    assert _extract_text(payload, "m", JIRA_FROM) == "plain"


def test_oversize_falls_back_and_pathological_input_is_fast():
    big = R + "x" * (512 * 1024) + "</span>"
    assert not has_diff_markup(big)
    t = time.monotonic()
    html_to_text(big, jira=True)
    html_to_text("<p>" * 150000 + R + "x</span>", jira=True)
    assert time.monotonic() - t < 5


def test_synthesis_and_triage_rules_cover_removed_text_and_ready_status():
    from brain_search.authorship import AUTHORSHIP_RULES
    from brain_triage.prompts import TRIAGE_PROMPT_TEMPLATE

    assert "[удалено: …]" in AUTHORSHIP_RULES
    assert "Ready to Prod" in AUTHORSHIP_RULES
    assert "готовность, а не выкатку" in AUTHORSHIP_RULES
    assert "[удалено: …]" in TRIAGE_PROMPT_TEMPLATE
    assert "Ready to Prod" in TRIAGE_PROMPT_TEMPLATE
