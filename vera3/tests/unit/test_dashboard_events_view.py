"""«Входящее» и карточка события: автор вместо аккаунта ингеста, тело вместо
сырого заголовка, кликабельная строка, читаемая карточка. Данные синтетические."""
from __future__ import annotations

from datetime import datetime

from dashboard.event_text import parse_content
from dashboard.event_view import event_card, header_pairs, transcript_html
from dashboard.events_view import event_row

TG = ("Author: Иван Тестов [counterparty]\nFrom: Иван Тестов\nChat: Тестовый чат (group)\n"
      "Date: 2026-10-03T14:30:04+00:00\nDirection: received\n---\nпервая строка\nвторая строка")


def _row(**over):
    base = {"id": 7, "source": "telegram", "account": "userbot", "category": "chat",
            "occurred_at": datetime(2026, 10, 3, 14, 30), "importance": 40,
            "nature": "fact", "project": "p", "triage_status": "done",
            "triage_error": None, "content_text": TG, "content_extra": None,
            "metadata": {"chat_title": "Тестовый чат"}, "request_id": None, "model": None,
            "tokens_in": None, "tokens_out": None, "cost_usd": None, "has_emb": False}
    base.update(over)
    return base


class TestEventRow:
    def test_shows_author_not_ingest_account(self):
        html = event_row(_row(), tech=False)
        assert "Иван Тестов" in html and "userbot" not in html

    def test_chat_is_muted_under_the_author(self):
        assert '<div class="muted small">Тестовый чат</div>' in event_row(_row(), tech=False)

    def test_text_is_body_only_on_one_line(self):
        html = event_row(_row(), tech=False)
        assert "первая строка вторая строка" in html
        assert "Author:" not in html and "Direction" not in html and "---" not in html

    def test_row_is_clickable_and_keeps_a_plain_link(self):
        html = event_row(_row(), tech=False)
        assert 'data-href="/events/7"' in html and 'href="/events/7"' in html

    def test_gmail_without_body_falls_back_to_subject(self):
        text = "From: Shop <s@example.com>\nSubject: Заказ 42\nDirection: received\n---\n"
        html = event_row(_row(source="gmail", content_text=text, metadata={}), tech=False)
        assert "Shop" in html and "Заказ 42" in html

    def test_event_without_header_uses_source_title(self):
        html = event_row(_row(source="claude", content_text="факт о чём-то", metadata={}),
                         tech=False)
        assert "Claude" in html and "факт о чём-то" in html

    def test_untrusted_values_are_escaped(self):
        text = "From: <script>x</script>\nChat: <b>c</b>\n---\n<img src=x onerror=1>"
        html = event_row(_row(content_text=text, metadata={}), tech=False)
        assert "<script>" not in html and "<b>" not in html and "<img" not in html


class TestEventCard:
    def test_header_as_key_value_block_and_body_with_breaks(self):
        html = event_card(_row())
        assert '<dl class="kv">' in html and "<dt>От</dt><dd>Иван Тестов</dd>" in html
        assert "<dt>Направление</dt><dd>входящее</dd>" in html
        assert 'class="body-text">первая строка\nвторая строка<' in html

    def test_date_header_is_replaced_by_local_time(self):
        html = event_card(_row())
        assert "2026-10-03T14:30:04+00:00" not in html and "<time" in html

    def test_service_fields_are_collapsed(self):
        html = event_card(_row())
        assert "<summary>Служебное</summary>" in html and "userbot" in html

    def test_transcript_is_collapsible(self):
        extra = {"kind": "voice_transcript", "chars": 5,
                 "utterances": [{"at": 0, "stream": "mic", "text": "привет"}]}
        html = event_card(_row(content_extra=extra))
        assert "<details" in html and "Стенограмма (1 реплик, 5 символов)" in html
        assert transcript_html(extra) in html

    def test_error_and_empty_body(self):
        html = event_card(_row(content_text="", triage_error="boom <b>"))
        assert "текста нет" in html and "boom &lt;b&gt;" in html

    def test_header_pairs_skip_author_when_from_present(self):
        labels = [k for k, _ in header_pairs(parse_content(TG), "Иван Тестов")]
        assert "Автор" not in labels and "Чат" in labels
