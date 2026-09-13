"""Фильтры `/events` строятся из данных.

13.09.2026: список источников был прибит в HTML (gmail/telegram/instagram/
monitor), и события слушателя (`voice`), Slack, Trello, Claude в журнале было
не отфильтровать — владелец не находил свои записанные разговоры.
"""
from __future__ import annotations

from dashboard.events_filters import source_options, status_options
from dashboard.events_routes import TRIAGE_STATUS_INFO


def test_every_source_with_events_is_offered_with_its_catalog_title():
    html = source_options([("telegram", 300000), ("voice", 113), ("slack", 900)], None)
    assert 'value="voice"' in html
    assert "Разговоры у ноутбука" in html          # подпись из каталога, не голый ключ
    assert 'value="slack"' in html and 'value="telegram"' in html


def test_order_follows_the_data_not_a_hardcoded_list():
    html = source_options([("voice", 5), ("gmail", 3)], None)
    assert html.index('value="voice"') < html.index('value="gmail"')


def test_selected_source_stays_selected_even_without_events():
    """Иначе фильтр молча сбрасывался бы в «все» и показывал не то."""
    html = source_options([("telegram", 10)], "monitor")
    assert '<option value="monitor" selected>' in html
    assert '<option value="" selected>' not in html


def test_unknown_source_is_shown_not_hidden():
    html = source_options([("brand_new", 7)], None)
    assert 'value="brand_new"' in html


def test_all_label_selected_when_no_filter():
    assert '<option value="" selected>' in source_options([("gmail", 1)], None)


def test_values_and_labels_are_escaped():
    html = source_options([('x"><script>', 1)], None)
    assert "<script>" not in html


def test_status_options_cover_every_triage_status():
    """Раньше были только done/pending/error — media_pending, dead, superseded
    отфильтровать было нельзя, а очередь распознавания живёт именно в media_pending."""
    html = status_options(TRIAGE_STATUS_INFO, "media_pending")
    for status in TRIAGE_STATUS_INFO:
        assert f'value="{status}"' in html
    assert '<option value="media_pending" selected>' in html


def test_thousands_separator_does_not_touch_the_label_itself():
    html = source_options([("a,b", 1234567)], None)
    assert "(a,b)" in html
    assert "1 234 567" in html
