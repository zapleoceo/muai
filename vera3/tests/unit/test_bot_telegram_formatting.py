"""Тесты форматирования Telegram-ответов — без aiogram (см. formatting.py)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "bot-telegram", "src"))

from bot_telegram.formatting import (  # noqa: E402
    TELEGRAM_MAX_CHARS,
    format_error,
    format_reply,
    plain_fallback,
)


class TestFormatReplyEscaping:
    def test_incident_2026_07_03_email_header_does_not_break(self):
        # Точное воспроизведение: LLM процитировал заголовок письма с email
        # в угловых скобках — раньше это ломало отправку в Telegram.
        raw = 'Пришло письмо From: IT Step <no-reply@itstep.org> с отчётом.'
        result = format_reply(raw, "cerebras", 0.0, 15, 9)
        assert "<no-reply@itstep.org>" not in result
        assert "&lt;no-reply@itstep.org&gt;" in result

    def test_angle_brackets_escaped(self):
        result = format_reply("текст <script>alert(1)</script>", "p", 0.0, 1, 0)
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_ampersand_escaped(self):
        result = format_reply("Tom & Jerry", "p", 0.0, 1, 0)
        assert "Tom &amp; Jerry" in result

    def test_provider_escaped_too(self):
        result = format_reply("ok", "prov<x>", 0.0, 1, 0)
        assert "<x>" not in result

    def test_footer_italic_tags_survive(self):
        # Собственные <i> теги footer'а — не экранируются, это разметка бота
        result = format_reply("ok", "cerebras", 0.001, 5, 2)
        assert "<i>via cerebras" in result
        assert result.endswith("</i>")


class TestFormatReplyContent:
    def test_empty_answer_placeholder(self):
        result = format_reply("", "p", 0.0, 0, 0)
        assert "(пустой ответ)" in result

    def test_none_provider_shows_dash(self):
        result = format_reply("ok", None, 0.0, 1, 0)
        assert "via —" in result

    def test_cost_and_counts_present(self):
        result = format_reply("ok", "cerebras", 0.1234, 15, 9)
        assert "$0.1234" in result
        assert "15 событий" in result
        assert "9 реплик контекста" in result


class TestFormatReplyLength:
    def test_never_exceeds_telegram_limit(self):
        long_answer = "а" * 10_000
        result = format_reply(long_answer, "cerebras", 0.0, 1, 0)
        assert len(result) <= TELEGRAM_MAX_CHARS

    def test_short_answer_not_truncated(self):
        result = format_reply("короткий ответ", "p", 0.0, 1, 0)
        assert "короткий ответ" in result
        assert "…" not in result

    def test_truncation_marker_present_for_long(self):
        result = format_reply("а" * 10_000, "cerebras", 0.0, 1, 0)
        assert "…" in result

    def test_escaping_before_truncation_stays_within_limit(self):
        # Много '<' — после escape каждый раздувается в 4 символа ('&lt;').
        # Обрезка должна учитывать длину УЖЕ экранированного текста.
        result = format_reply("<" * 5000, "cerebras", 0.0, 1, 0)
        assert len(result) <= TELEGRAM_MAX_CHARS
        # И не должно быть "разорванной" сущности вроде "&l" на границе среза
        assert "&l…" not in result and "&lt…" not in result


class TestPlainFallback:
    def test_no_html_tags_in_output(self):
        result = plain_fallback("текст <b>с тегами</b> & амперсандом", "cerebras")
        # plain_fallback НЕ экранирует — он для parse_mode=None, где теги
        # и так не интерпретируются Telegram'ом
        assert "via cerebras" in result

    def test_truncates_to_4000(self):
        result = plain_fallback("а" * 10_000, "p")
        assert len(result) <= 4000 + 20  # + "\n\nvia p"

    def test_none_provider(self):
        result = plain_fallback("ok", None)
        assert "via —" in result

    def test_empty_answer(self):
        result = plain_fallback("", "p")
        assert "(пустой ответ)" in result


class TestFormatError:
    def test_contains_exception_message(self):
        result = format_error(ValueError("boom"))
        assert "boom" in result
        assert result.startswith("⚠ Ошибка:")

    def test_truncated_to_4000(self):
        result = format_error(ValueError("x" * 10_000))
        assert len(result) <= 4000

    def test_no_html_wrapping(self):
        # format_error всегда шлётся с parse_mode=None у вызывающего —
        # сама функция не обязана экранировать, но не должна добавлять теги
        result = format_error(ValueError("<tag>"))
        assert result.count("<") == 1  # только из текста исключения
