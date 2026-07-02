"""Тесты реестра настроек (control.SETTINGS) — то что редактируется из дашборда."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from vera_shared.control import (  # noqa: E402
    MONITOR_BACKLOG_ENABLED,
    MONITOR_THROTTLE_MIN,
    SETTINGS,
    TRIAGE_BACKLOG_HUGE,
    TRIAGE_BACKLOG_WARN,
)


def _by_key(key):
    return next((s for s in SETTINGS if s.key == key), None)


class TestRegistry:
    def test_all_settings_have_desc_and_default(self):
        for s in SETTINGS:
            assert s.label and s.desc, f"{s.key} missing label/desc"
            assert s.default != "", f"{s.key} missing default"
            assert s.kind in ("int", "bool")

    def test_keys_unique(self):
        keys = [s.key for s in SETTINGS]
        assert len(keys) == len(set(keys))

    def test_monitor_throttle_present(self):
        s = _by_key(MONITOR_THROTTLE_MIN)
        assert s is not None
        assert s.default == "30"
        assert s.unit == "мин"

    def test_backlog_thresholds_present(self):
        assert _by_key(TRIAGE_BACKLOG_WARN).default == "5000"
        assert _by_key(TRIAGE_BACKLOG_HUGE).default == "10000"

    def test_backlog_toggle_is_bool(self):
        s = _by_key(MONITOR_BACKLOG_ENABLED)
        assert s.kind == "bool"
        assert s.default == "1"

    def test_defaults_match_prior_hardcoded_values(self):
        # То что раньше было захардкожено в vera3-monitor.sh
        assert _by_key(MONITOR_THROTTLE_MIN).default == "30"
        assert _by_key(TRIAGE_BACKLOG_WARN).default == "5000"
        assert _by_key(TRIAGE_BACKLOG_HUGE).default == "10000"
