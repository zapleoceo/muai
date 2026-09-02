"""Live-прогресс: два ETA по двум разным темпам и честные полосы.

02.09.2026 дашборд показывал «В очереди на триаж 442, ETA ~3.3 ч», хотя
очередь на триаж была ПУСТА, а 442 — фото, ждущие vision. ETA считался как
`весь backlog / темп триажа` (136/час), тогда как фото идут через локальную
модель со скоростью ~12/час: 3.3 часа против настоящих 36.

Полоса «Прогресс триажа (обработано / весь backlog)» брала знаменателем
работу за последние сутки, поэтому росла, когда Вера больше работала, и
падала, когда крон доливал очередь.
"""
from __future__ import annotations

from dashboard.progress_routes import _eta, _pct


class TestEta:
    def test_uses_the_rate_of_its_own_queue(self):
        # 442 фото при 12 распознаваниях в час — это сутки с половиной,
        # а не три часа темпом триажа
        assert _eta(442, 12.4) == "~35.6 ч"
        assert _eta(442, 136) == "~3.2 ч"

    def test_minutes_below_two_hours(self):
        assert _eta(30, 60) == "~30 мин"

    def test_days_beyond_two_days(self):
        assert _eta(19101, 12.4) == "~64.2 дн"

    def test_no_rate_is_not_infinity(self):
        assert _eta(500, 0) == "—"

    def test_empty_queue_has_no_eta(self):
        assert _eta(0, 12.4) == "—"


class TestProgressBar:
    def test_measures_done_against_the_whole_volume(self):
        assert _pct(433808, 433808 + 442) == 99

    def test_empty_volume_does_not_divide_by_zero(self):
        assert _pct(0, 0) == 0

    def test_nothing_done_is_zero_not_full(self):
        """Старая формула при пустом суточном окне рисовала полную полосу."""
        assert _pct(0, 19101) == 0

    def test_never_exceeds_full(self):
        assert _pct(120, 100) == 100
