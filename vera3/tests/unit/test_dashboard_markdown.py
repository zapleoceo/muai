"""Безопасное подмножество markdown для ответа поиска: форматирование есть,
сырого HTML от модели нет."""
from __future__ import annotations

import pytest
from dashboard.ui.markdown import render_markdown


class TestFormatting:
    def test_bold_italic_and_code(self):
        out = render_markdown("**Катерина Кравченко**, *важно* и `x = 1`")
        assert out == ("<strong>Катерина Кравченко</strong>, <em>важно</em> "
                       "и <code>x = 1</code>")

    def test_bullets_become_one_list(self):
        out = render_markdown("Итог:\n- первое\n- второе\n* третье")
        assert out.count("<li>") == 3 and out.count("<ul>") == 1
        assert out.startswith("Итог:<br><ul>")

    def test_line_breaks_and_paragraphs(self):
        assert render_markdown("а\nб") == "а<br>б"
        assert render_markdown("а\n\nб") == "а<br><br>б"

    def test_heading_is_bold_not_a_tag(self):
        assert render_markdown("## Заголовок") == "<strong>Заголовок</strong>"

    def test_snake_case_is_not_italic(self):
        assert "<em>" not in render_markdown("поле my_field_name заполнено")

    def test_code_content_is_not_formatted(self):
        assert render_markdown("`**не жирный**`") == "<code>**не жирный**</code>"

    def test_empty(self):
        assert render_markdown("") == ""


class TestLinks:
    def test_https_link_is_rendered_with_safe_rel(self):
        out = render_markdown("[сайт](https://example.com/a?b=1&c=2)")
        assert 'href="https://example.com/a?b=1&amp;c=2"' in out
        assert 'rel="noopener noreferrer"' in out and 'target="_blank"' in out

    @pytest.mark.parametrize("url", [
        "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<b>x</b>",
        "vbscript:x", "//evil.example", "ftp://x"])
    def test_non_http_links_stay_plain_text(self, url):
        out = render_markdown(f"[x]({url})")
        assert "<a " not in out and "href" not in out


class TestXss:
    @pytest.mark.parametrize("payload", [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "**<b onclick=x>**",
        "[<script>](https://a.b)",
        "`<script>`",
        "<a href=\"javascript:alert(1)\">x</a>",
    ])
    def test_model_html_never_reaches_the_page(self, payload):
        out = render_markdown(payload)
        assert "<script" not in out and "<img" not in out and "<b " not in out
        assert '<a href="javascript' not in out

    def test_attribute_breakout_in_url_is_escaped(self):
        out = render_markdown('[x](https://a.b/"onmouseover="alert(1))')
        assert 'onmouseover="' not in out

    def test_stash_marker_in_input_cannot_pull_foreign_html(self):
        out = render_markdown("\x000\x00 `<b>`")
        assert "\x00" not in out
