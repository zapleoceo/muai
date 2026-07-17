"""brain_search.reports — точная помесячная агрегация SMS-дайджестов.

Чистые функции (парсинг, детект, рендер) — без БД."""
from __future__ import annotations

from brain_search.reports import (
    _is_snapshot_key,
    _parse_kv,
    detect_report_request,
    detect_target_field,
    render_report_markdown,
    render_simple_markdown,
)

# ─── detect_report_request / detect_target_field ────────────────────────────


def test_detect_report_request_with_year():
    assert detect_report_request("отчёт заказов помесячно за 2026 год") == (True, 2026)


def test_detect_report_request_without_year():
    assert detect_report_request("статистика по месяцам") == (True, None)


def test_detect_report_request_negative():
    assert detect_report_request("что мы решили про NAS?") == (False, None)


def test_detect_target_field_orders_maps_to_bn():
    assert detect_target_field("отчёт заказов за 2026") == "b/n"
    assert detect_target_field("orders monthly") == "b/n"


def test_detect_target_field_detailed_overrides():
    assert detect_target_field("отчёт заказов детально") is None
    assert detect_target_field("покажи всё") is None


# ─── _parse_kv ──────────────────────────────────────────────────────────────


def test_parse_kv_reads_lines_after_separator():
    body = ("Ежедневный отчёт\n---\n"
            "b/n : 12 500 000\n"
            "contract: 3\n"
            "ost, IDR: -1 250 000.50\n")
    kv = _parse_kv(body)
    assert kv["b/n"] == 12500000.0
    assert kv["contract"] == 3.0
    assert kv["ost, idr"] == -1250000.5


def test_parse_kv_free_text_yields_nothing():
    assert _parse_kv("сегодня продали два контракта, всё хорошо") == {}
    assert _parse_kv("текст без разделителя b/n : 100") == {}   # нет '---'


def test_is_snapshot_key():
    assert _is_snapshot_key("ost, idr") is True
    assert _is_snapshot_key("lead point") is True
    assert _is_snapshot_key("b/n") is False
    assert _is_snapshot_key("contract") is False


# ─── рендеры ────────────────────────────────────────────────────────────────


def _report(**over):
    base = {
        "chat_title": "Jakarta: sms report",
        "year": 2026,
        "total_messages": 4,
        "months": {
            "2026-01": {"b/n": 100.0, "ost": 50.0},
            "2026-02": {"b/n": 200.0, "ost": 70.0},
        },
        "counts": {"2026-01": 2, "2026-02": 2},
        "unstructured": {"2026-01": 1},
        "keys": ["b/n", "ost"],
    }
    base.update(over)
    return base


def test_render_simple_sums_flow_field():
    md = render_simple_markdown(_report(), "b/n")
    assert "| 2026-01 | 100 |" in md
    assert "| **Итого** | **300** |" in md          # 100+200, сумма потока
    assert "1 сообщени(й) без числовых полей" in md


def test_render_simple_snapshot_field_takes_last_value():
    md = render_simple_markdown(_report(), "ost")
    assert "| **Итого** | **70** |" in md           # последний, НЕ 120


def test_render_simple_missing_field():
    md = render_simple_markdown(_report(), "nope")
    assert "не нашлось поля «nope»" in md


def test_render_simple_empty_report():
    md = render_simple_markdown(_report(total_messages=0, months={}), "b/n")
    assert "нет сообщений за 2026 год" in md


def test_render_full_table_totals_and_snapshot_label():
    md = render_report_markdown(_report())
    assert "ost (на конец мес.)" in md              # снэпшот подписан
    assert "| **Итого** | 4 | **300** | **70** |" in md
    assert "свободный текст, не входят в суммы" in md


def test_render_full_empty_report():
    md = render_report_markdown(_report(total_messages=0, months={}, year=None))
    assert md.endswith("нет сообщений.")
