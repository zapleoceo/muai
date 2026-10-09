"""Диаграммы Ганта `/tasks`: серверный SVG, фактические отрезки и отдельная полоса плана.

Подписи оси — `<time data-utc>` (часовой пояс браузера, как везде в дашборде), поэтому
они лежат HTML-слоем над SVG; в `<title>` отрезков время в UTC. Весь текст экранируется.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from vera_shared.room.intervals import (
    BLOCKED,
    PAUSED,
    REVIEW,
    UNKNOWN,
    WAITING,
    WORK,
    Segment,
)

from dashboard.render import esc, local_dt

SPANS = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
DEFAULT_SPAN = "24h"
FIT = "fit"
FIT_LABEL = "по данным"
FIT_MIN = timedelta(hours=1)
FIT_DEFAULT_WITHIN = timedelta(hours=6)
KIND_LABELS = {WORK: "работа", PAUSED: "пауза", REVIEW: "проверка", BLOCKED: "блок",
               WAITING: "ожидание", UNKNOWN: "неизвестно"}
_FILL = {WORK: "var(--ok)", PAUSED: "var(--faint)", REVIEW: "var(--accent)",
         BLOCKED: "var(--err)", WAITING: "var(--warn)", UNKNOWN: "var(--line-strong)"}
_STEPS = tuple(timedelta(minutes=m) for m in (1, 5, 15, 30, 60, 180, 360, 720, 1440, 4320))
_W = 1000
MAX_TICKS = 8

CSS = """<style>
.gt{margin:.6rem 0 1rem}.gt-scroll{overflow-x:auto}.gt-in{min-width:640px}
.gt-grp{font-size:.75rem;font-weight:600;color:var(--text-strong);margin:.5rem 0 .1rem}
.gt-row{display:flex;align-items:center;gap:.5rem}
.gt-lab{flex:0 0 9rem;font-size:.8rem;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gt-row svg{flex:1;height:20px;display:block}
.gt-axis{position:relative;height:1.2rem;margin-left:9.5rem;font-size:.7rem;color:var(--faint)}
.gt-axis span{transform:translateX(-50%);white-space:nowrap}
.gt-leg{display:flex;flex-wrap:wrap;gap:.2rem .9rem;font-size:.75rem;color:var(--muted);margin-top:.3rem}
.gt-leg i{display:inline-block;width:.7rem;height:.7rem;border-radius:2px;margin-right:.3rem;vertical-align:-1px}
.gt-leg i.plan{background:none;border:1px solid var(--muted)}
</style>"""


@dataclass(frozen=True)
class GanttRow:
    label: str
    segments: list[Segment]
    plan: tuple[datetime, datetime] | None = None
    group: str = ""


def parse_span(value: str | None) -> str:
    return value if value in SPANS or value == FIT else DEFAULT_SPAN


def resolve_span(value: str | None, rows: list[GanttRow], now: datetime) -> str:
    """Явный `?span=` главнее; без него — fit, если вся активность моложе 6 ч, иначе 24h."""
    if value in SPANS or value == FIT:
        return str(value)
    starts = [s.start for r in rows for s in r.segments]
    if starts and min(starts) >= now - FIT_DEFAULT_WITHIN:
        return FIT
    return DEFAULT_SPAN


def fit_start(rows: list[GanttRow], now: datetime) -> datetime:
    starts = [s.start for r in rows for s in r.segments if s.start < now]
    return min(min(starts, default=now), now - FIT_MIN)


def _ticks(t0: datetime, t1: datetime) -> list[datetime]:
    span = t1 - t0
    step = next((s for s in _STEPS if span / s <= MAX_TICKS), _STEPS[-1])
    epoch = datetime(1970, 1, 1)
    first = epoch + ((t0 - epoch) // step + 1) * step
    out = []
    while first < t1:
        out.append(first)
        first += step
    return out


def _x(t: datetime, t0: datetime, t1: datetime) -> float:
    return max(0.0, min(1.0, (t - t0) / (t1 - t0))) * _W


def _title(s: Segment) -> str:
    who = s.agent + (f"/{s.session}" if s.session else "")
    return f"{who} · {KIND_LABELS[s.kind]}: {s.start:%Y-%m-%d %H:%M}–{s.end:%Y-%m-%d %H:%M} UTC"


def _rect(s: Segment, t0: datetime, t1: datetime) -> str:
    start, end = max(s.start, t0), min(s.end, t1)
    if end <= start:
        return ""
    x, x2 = _x(start, t0, t1), _x(end, t0, t1)
    return (f'<rect x="{x:.1f}" y="2" width="{max(x2 - x, 1):.1f}" height="11" rx="2" '
            f'fill="{_FILL[s.kind]}"><title>{esc(_title(s))}</title></rect>')


def _plan_rect(plan: tuple[datetime, datetime], t0: datetime, t1: datetime) -> str:
    start, end = max(plan[0], t0), min(plan[1], t1)
    if end <= start:
        return ""
    x, x2 = _x(start, t0, t1), _x(end, t0, t1)
    text = f"план: {plan[0]:%Y-%m-%d %H:%M}–{plan[1]:%Y-%m-%d %H:%M} UTC"
    return (f'<rect x="{x + .5:.1f}" y="15.5" width="{max(x2 - x - 1, 1):.1f}" height="3" '
            f'fill="none" stroke="var(--muted)" stroke-width="1"><title>{esc(text)}</title></rect>')


def _svg(row: GanttRow, t0: datetime, t1: datetime, ticks: list[datetime]) -> str:
    grid = "".join(
        f'<line x1="{_x(t, t0, t1):.1f}" x2="{_x(t, t0, t1):.1f}" y1="0" y2="20" '
        f'stroke="var(--line)" vector-effect="non-scaling-stroke"/>' for t in ticks)
    bars = "".join(_rect(s, t0, t1) for s in row.segments)
    plan = _plan_rect(row.plan, t0, t1) if row.plan else ""
    return (f'<svg viewBox="0 0 {_W} 20" preserveAspectRatio="none" role="img" '
            f'aria-label="{esc(row.label)}">{grid}{bars}{plan}</svg>')


def _axis(ticks: list[datetime], t0: datetime, t1: datetime, fmt: str) -> str:
    items = "".join(f'<span style="position:absolute;left:{_x(t, t0, t1) / _W * 100:.2f}%">'
                    f'{local_dt(t, fmt)}</span>' for t in ticks)
    return f'<div class="gt-axis">{items}</div>'


def _legend(with_plan: bool) -> str:
    items = "".join(f'<span><i style="background:{_FILL[k]}"></i>{KIND_LABELS[k]}</span>'
                    for k in KIND_LABELS)
    plan = '<span><i class="plan"></i>план</span>' if with_plan else ""
    return f'<div class="gt-leg">{items}{plan}</div>'


def _chart(rows: list[GanttRow], t0: datetime, t1: datetime, fmt: str) -> str:
    ticks = _ticks(t0, t1)
    parts: list[str] = []
    group: str | None = None
    for r in rows:
        if r.group and r.group != group:
            parts.append(f'<div class="gt-grp">{esc(r.group)}</div>')
        group = r.group
        parts.append(f'<div class="gt-row"><div class="gt-lab" title="{esc(r.label)}">'
                     f'{esc(r.label)}</div>{_svg(r, t0, t1, ticks)}</div>')
    body = "".join(parts)
    return (f'<div class="gt-scroll"><div class="gt-in">{body}{_axis(ticks, t0, t1, fmt)}'
            f'</div></div>{_legend(any(r.plan for r in rows))}')


def _lane_label(s: Segment) -> str:
    return s.agent + (f" · {s.session[:8]}" if s.session else "")


def task_gantt(segments: list[Segment], plan: tuple[datetime, datetime] | None) -> str:
    """Карточка задачи: дорожка на каждого агента+сессию и своя строка плана."""
    if plan and plan[1] <= plan[0]:
        plan = None
    if not segments and not plan:
        return '<p class="muted">Интервалов выполнения пока нет</p>'
    lanes: dict[tuple[str, str], list[Segment]] = {}
    for s in segments:
        lanes.setdefault((s.agent, s.session), []).append(s)
    rows = [GanttRow(_lane_label(v[0]), v) for v in lanes.values()]
    if plan:
        rows.append(GanttRow("план", [], plan))
    starts = [s.start for s in segments] + ([plan[0]] if plan else [])
    ends = [s.end for s in segments] + ([plan[1]] if plan else [])
    t0, t1 = min(starts), max(ends)
    if t1 <= t0:
        t1 = t0 + timedelta(minutes=1)
    fmt = "time" if t1 - t0 <= timedelta(days=1) else "datetime"
    return f'<div class="gt"><h3>Выполнение</h3>{_chart(rows, t0, t1, fmt)}</div>'


def span_nav(tab: str, span: str) -> str:
    chips = "".join(f'<a class="chip{" on" if k == span else ""}" href="/tasks?tab={esc(tab)}'
                    f'&amp;span={k}">{FIT_LABEL if k == FIT else k}</a>' for k in (*SPANS, FIT))
    return f'<div class="chips">{chips}</div>'


def tasks_gantt(rows: list[GanttRow], now: datetime, span: str, tab: str) -> str:
    """Сводка по задачам вкладки за окно `span`; строки без отрезков в окне не рисуются."""
    t1 = now
    t0 = fit_start(rows, now) if span == FIT else now - SPANS[parse_span(span)]
    visible = [r for r in rows
               if any(s.end > t0 and s.start < t1 for s in r.segments)
               or (r.plan and r.plan[1] > t0 and r.plan[0] < t1)]
    fmt = "time" if parse_span(span) == "24h" else "date"
    body = (_chart(visible, t0, t1, fmt) if visible
            else '<p class="muted">В этом окне интервалов нет</p>')
    return f'<div class="gt">{span_nav(tab, parse_span(span))}{body}</div>'
