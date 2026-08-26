"""Состояние для трея и подсказка над иконкой.

Смысл иконки один: ответить на «оно вообще работает?» без чтения логов.
Поэтому проверяем именно то, что цвет и текст не врут о состоянии.
"""
from __future__ import annotations

from vera_listener import tray
from vera_listener.status import DEAF, IDLE, OFFLINE, TALKING, Status


class TestStatus:
    def test_starts_idle(self):
        assert Status().snapshot()["state"] == IDLE

    def test_conversation_switches_to_talking(self):
        s = Status()
        s.set_state(TALKING)
        assert s.snapshot()["state"] == TALKING

    def test_successful_send_counts_and_returns_to_idle(self):
        s = Status()
        s.set_state(TALKING)
        s.note_sent(2, 0)
        snap = s.snapshot()
        assert (snap["sent"], snap["queued"], snap["state"]) == (2, 0, IDLE)
        assert snap["since_sent_s"] is not None

    def test_queue_left_means_offline(self):
        """Очередь не разошлась — значит сети нет, и это должно быть видно."""
        s = Status()
        s.note_sent(0, 3)
        snap = s.snapshot()
        assert (snap["state"], snap["queued"]) == (OFFLINE, 3)

    def test_send_clears_the_previous_error(self):
        s = Status()
        s.note_error("ConnectionError: сеть недоступна")
        s.note_sent(1, 0)
        assert s.snapshot()["last_error"] is None

    def test_error_is_capped_not_unbounded(self):
        s = Status()
        s.note_error("x" * 5000)
        assert len(s.snapshot()["last_error"]) == 200

    def test_dropped_is_counted_separately_from_sent(self):
        """Отсеянное как «не разговор» — не потеря и не отправка."""
        s = Status()
        s.note_dropped()
        s.note_dropped()
        snap = s.snapshot()
        assert (snap["dropped"], snap["sent"]) == (2, 0)


class TestTooltip:
    def _snap(self, **over):
        base = {"state": IDLE, "queued": 0, "sent": 0, "dropped": 0,
                "last_error": None, "uptime_s": 10.0, "since_sent_s": None}
        base.update(over)
        return base

    def test_idle_says_it_is_listening(self):
        assert tooltip_head(self._snap()) == "Вера слушает"

    def test_talking_says_it_is_recording(self):
        assert tooltip_head(self._snap(state=TALKING)) == "Вера пишет разговор"

    def test_offline_is_named_as_no_network(self):
        assert "нет сети" in tooltip_head(self._snap(state=OFFLINE))

    def test_deaf_does_not_look_like_working(self):
        """Красное состояние обязано читаться как проблема, а не как «слушаю»."""
        head = tooltip_head(self._snap(state=DEAF))
        assert "не слышит" in head

    def test_counts_are_shown(self):
        text = tray.tooltip(self._snap(sent=7, queued=2))
        assert "отправлено: 7" in text
        assert "в очереди: 2" in text

    def test_error_is_shown_when_present(self):
        text = tray.tooltip(self._snap(last_error="HTTP 500"))
        assert "HTTP 500" in text

    def test_dropped_is_hidden_when_zero(self):
        assert "отсеяно" not in tray.tooltip(self._snap())
        assert "отсеяно" in tray.tooltip(self._snap(dropped=3))

    def test_never_sent_says_so_instead_of_lying(self):
        assert "ещё ничего" in tray.tooltip(self._snap())


def tooltip_head(snap: dict) -> str:
    return tray.tooltip(snap).split("\n")[0]


class TestHuman:
    def test_just_now(self):
        assert tray._human(5) == "только что"

    def test_minutes(self):
        assert tray._human(600) == "10 мин назад"

    def test_hours(self):
        assert tray._human(3600 * 3) == "3 ч назад"

    def test_never(self):
        assert tray._human(None) == "ещё ничего"


class TestGracefulDegradation:
    def test_missing_pystray_is_not_a_crash(self, monkeypatch, tmp_path):
        """Иконка не важнее записи разговоров: нет pystray — работаем без неё."""
        import builtins
        real_import = builtins.__import__

        def no_pystray(name, *a, **kw):
            if name == "pystray":
                raise ImportError("no pystray")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", no_pystray)
        shown = tray.run(Status(), log_file=tmp_path / "l.log",
                         queue_dir=tmp_path, on_quit=lambda: None)
        assert shown is False
