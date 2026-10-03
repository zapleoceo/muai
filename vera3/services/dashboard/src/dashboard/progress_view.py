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


PROGRESS_STYLE = """<style>
        .prog-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
                      gap:14px; margin:14px 0; }
        .prog-cell { background:var(--pico-background-color); border:1px solid var(--vera-line);
                      border-radius:10px; padding:14px; }
        .prog-label { font-size:11px; color:var(--vera-muted); text-transform:uppercase;
                       letter-spacing:0.05em; }
        .prog-big { font-size:26px; font-weight:600; margin:6px 0 3px; }
        .prog-unit { font-size:13px; color:var(--vera-muted); font-weight:400; margin-left:4px; }
        .bar { background:var(--pico-background-color); height:8px; border-radius:4px;
                overflow:hidden; border:1px solid var(--vera-line); }
        .bar-fill { background:linear-gradient(90deg,var(--pico-primary),var(--vera-ok));
                     height:100%; transition:width 1s ease; }
        .bf-control { display:flex; align-items:center; gap:12px; margin:6px 0 14px; }
        .bf-btn { width:auto; margin:0; padding:6px 16px; font-size:13px; }
        .bf-rate { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:0; }
        .bf-rate label { font-size:12px; color:var(--vera-muted); margin:0; }
        .bf-rate input { width:130px; margin:0; padding:6px 10px; }
        .bf-hint { font-size:12px; color:var(--vera-muted); }
      </style>"""
