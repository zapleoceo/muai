"""dashboard.render.local_dt — server emits a UTC-tagged <time> element that
client JS converts to the viewer's local timezone. These test the server half
(the JS itself is exercised in the browser, not here)."""
from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from dashboard.render import _FMT_FALLBACK, _render, local_dt  # noqa: E402


def test_none_returns_empty_placeholder():
    assert local_dt(None) == "—"
    assert local_dt(None, "date", empty="никогда") == "никогда"


def test_naive_datetime_tagged_as_utc():
    """DB datetimes are naive UTC — the emitted data-utc MUST carry a Z, or the
    browser would parse it as local time and double-shift."""
    html = local_dt(datetime(2026, 7, 11, 13, 1, 17), "datetime")
    assert 'data-utc="2026-07-11T13:01:17Z"' in html
    assert 'data-fmt="datetime"' in html
    assert html.startswith("<time ") and html.endswith("</time>")


def test_aware_datetime_not_double_tagged():
    """An already-tz-aware datetime keeps its offset, no extra Z appended."""
    html = local_dt(datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc), "datetime")
    assert "+00:00" in html
    assert "Z<" not in html and 'Z"' not in html


def test_fallback_text_matches_requested_format():
    """The visible text (JS-off fallback) uses the server-side strftime for fmt."""
    dt = datetime(2026, 7, 11, 13, 1, 17)
    assert ">2026-07-11 13:01<" in local_dt(dt, "datetime")
    assert ">2026-07-11 13:01:17<" in local_dt(dt, "datetime_sec")
    assert ">2026-07-11<" in local_dt(dt, "date")
    assert ">13:01<" in local_dt(dt, "time")
    assert ">11 Jul 2026<" in local_dt(dt, "date_human")


def test_unknown_fmt_falls_back_to_datetime():
    html = local_dt(datetime(2026, 7, 11, 13, 1, 17), "bogus")
    assert ">2026-07-11 13:01<" in html
    assert 'data-fmt="bogus"' in html   # JS also defaults bogus → datetime


def test_all_fmt_keys_render():
    dt = datetime(2026, 1, 2, 3, 4, 5)
    for fmt in _FMT_FALLBACK:
        html = local_dt(dt, fmt)
        assert f'data-fmt="{fmt}"' in html
        assert "data-utc=" in html


def test_output_is_escaped():
    """fmt/iso flow through esc() — no attribute-breaking chars leak."""
    html = local_dt(datetime(2026, 7, 11, 13, 1, 17), 'x"onerror=1')
    assert '"onerror=1"' not in html   # the raw quote must be entity-escaped


def test_render_wires_in_localizer():
    """Every page must carry the tz converter + note, else <time> stays UTC."""
    page = _render("home", "<p>body</p>")
    assert "__localizeTimes" in page
    assert 'id="tz-note"' in page
    assert "htmx:afterSwap" in page   # live-progress fragment gets re-localized
