"""Разбор `content_text` на заголовок и тело, человеческие поля, строки
источников под ответом поиска. Примеры синтетические, но повторяют формат
ингесторов (telegram / gmail / slack / voice / claude / память агента)."""
from __future__ import annotations

from dashboard.event_text import clean_name, describe, normalize_subject, parse_content
from dashboard.search_view import SOURCES_SHOWN, sources_html

TELEGRAM = ("Author: Иван Тестов [counterparty]\nFrom: Иван (@ivan_t)\n"
            "Chat: Тестовый чат (group)\nDate: 2026-01-01T10:00:00+00:00\n"
            "Direction: received\n---\nкрабы 2кг из кассы")
GMAIL = ("Author: Shop <news@example.com> [counterparty]\nFrom: Shop <news@example.com>\n"
         "To: me@example.com\nSubject: Re: Заказ 42\nDate: Sat, 03 Jan 2026 07:22:49 -0700\n"
         "Direction: received\n---\nВаш заказ отправлен.\n\nСсылка: https://example.com/o/42")
SLACK = "Author: Анна Пример [counterparty]\nWhere: #proj (тред)\n---\nготово"


class TestParseContent:
    def test_telegram_header_and_body(self):
        p = parse_content(TELEGRAM)
        assert p.body == "крабы 2кг из кассы"
        assert p.headers["Chat"] == "Тестовый чат (group)"

    def test_gmail_body_keeps_line_breaks(self):
        assert parse_content(GMAIL).body.splitlines()[0] == "Ваш заказ отправлен."
        assert "\n\n" in parse_content(GMAIL).body

    def test_truncated_preview_without_separator_has_empty_body(self):
        p = parse_content("Author: A [counterparty]\nFrom: A\nDirection: recei")
        assert p.body == "" and p.headers["From"] == "A"

    def test_plain_fact_has_no_headers(self):
        p = parse_content("2026-01-01 Дима выбрал вариант A\nвторая строка")
        assert p.headers == {} and p.body.startswith("2026-01-01")

    def test_unknown_key_is_body_not_header(self):
        assert parse_content("Note: что-то\n---\nтекст").headers == {}

    def test_none_and_empty(self):
        assert parse_content(None).body == "" and parse_content("").headers == {}

    def test_separator_inside_body_does_not_split_again(self):
        assert parse_content("From: A\n---\nа\n---\nб").body == "а\n---\nб"


class TestDescribe:
    def test_telegram_shows_author_and_chat(self):
        line = describe(parse_content(TELEGRAM), {"chat_title": "Тестовый чат"})
        assert (line.who, line.venue) == ("Иван", "Тестовый чат")

    def test_telegram_without_meta_strips_chat_kind(self):
        assert describe(parse_content(TELEGRAM)).venue == "Тестовый чат"

    def test_gmail_uses_sender_name_and_subject(self):
        line = describe(parse_content(GMAIL))
        assert line.who == "Shop" and line.venue == "Re: Заказ 42"

    def test_slack_uses_author_and_where(self):
        line = describe(parse_content(SLACK))
        assert (line.who, line.venue) == ("Анна Пример", "#proj (тред)")

    def test_voice_falls_back_to_metadata(self):
        line = describe(parse_content("(разговор)\n\nГде: x"),
                        {"author_label": "Я", "window_title": "Окно"})
        assert (line.who, line.venue) == ("Я", "Окно")

    def test_claude_fact_has_nobody(self):
        line = describe(parse_content("факт"), {"kind": "decision"})
        assert line.who == "" and line.venue == ""


class TestHelpers:
    def test_clean_name_variants(self):
        assert clean_name('"Meta B" <n@x.y>') == "Meta B"
        assert clean_name("<n@x.y>") == "n@x.y"
        assert clean_name("Имя (@nick)") == "Имя"
        assert clean_name("(@nick)") == "@nick"
        assert clean_name("Автор [counterparty]") == "Автор"

    def test_subject_normalisation_drops_reply_prefixes(self):
        assert normalize_subject("RE: Fwd: Заказ 42") == normalize_subject("заказ 42")


def _hit(i, source="gmail", preview=GMAIL, at="2026-01-03T10:00:00"):
    return {"event_id": i, "source": source, "occurred_at": at, "content_preview": preview}


class TestSourcesHtml:
    def test_gmail_line_is_subject_and_sender(self):
        html = sources_html([_hit(1)])
        assert "Re: Заказ 42 · от Shop" in html
        assert "Author:" not in html and "To:" not in html and "Direction" not in html

    def test_telegram_line_is_author_chat_and_body_snippet(self):
        html = sources_html([_hit(1, "telegram", TELEGRAM)])
        assert "Иван · Тестовый чат" in html and "крабы 2кг из кассы" in html
        assert "From:" not in html

    def test_snippet_is_limited_to_one_line(self):
        long_body = "слово " * 100
        html = sources_html([_hit(1, "telegram", f"From: A\n---\n{long_body}")])
        assert "…" in html and "\n" not in html.split("muted small\">")[-1]

    def test_same_event_twice_is_one_source(self):
        assert sources_html([_hit(1), _hit(1)]).count("<li>") == 1

    def test_same_thread_in_a_row_is_deduplicated(self):
        thread = [_hit(1), _hit(2, preview=GMAIL.replace("Re: ", "")), _hit(3)]
        assert sources_html(thread).count("<li>") == 1

    def test_different_subject_is_kept(self):
        other = GMAIL.replace("Заказ 42", "Другое")
        assert sources_html([_hit(1), _hit(2, preview=other)]).count("<li>") == 2

    def test_limit_applies_after_dedup(self):
        hits = [_hit(i, "telegram", f"From: A\nChat: C{i}\n---\nтекст {i}") for i in range(1, 12)]
        assert sources_html(hits).count("<li>") == SOURCES_SHOWN

    def test_untrusted_headers_are_escaped(self):
        evil = "From: <script>alert(1)</script>\nSubject: <img src=x onerror=1>\n---\n<b>тело</b>"
        html = sources_html([_hit(1, preview=evil)])
        assert "<script>" not in html and "<img" not in html and "<b>" not in html
