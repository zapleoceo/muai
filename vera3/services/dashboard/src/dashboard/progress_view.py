"""Разметка живого прогресса: переключатель паузы, лимит запросов, стили."""
from __future__ import annotations


def pause_controls(paused: bool) -> str:
    if paused:
        return (
            '<span class="pill warn">⏸ Разбор на паузе</span>'
            '<button class="bf-btn" hx-post="/control/backfill" '
            'hx-vals=\'{"action":"resume"}\' hx-target="#live-progress" '
            'hx-swap="innerHTML">▶ Продолжить</button>'
        )
    return (
        '<span class="pill ok">▶ Разбор идёт</span>'
        '<button class="bf-btn secondary" hx-post="/control/backfill" '
        'hx-vals=\'{"action":"pause"}\' hx-target="#live-progress" '
        'hx-swap="innerHTML">⏸ Пауза</button>'
    )


def rate_controls(max_per_hour: int) -> str:
    rate_val = "" if max_per_hour <= 0 else str(max_per_hour)
    rate_hint = ("без лимита" if max_per_hour <= 0
                 else f"≈ {max(1, round(max_per_hour / 60))}/мин равномерно")
    return (
        '<form class="bf-rate" hx-post="/control/backfill-rate" '
        'hx-target="#live-progress" hx-swap="innerHTML">'
        '<label>Не больше запросов в час:</label>'
        f'<input type="number" name="max_per_hour" min="0" step="50" '
        f'value="{rate_val}" placeholder="пусто = без лимита">'
        '<button class="bf-btn" type="submit">Сохранить</button>'
        f'<span class="bf-hint">{rate_hint}</span></form>'
    )

