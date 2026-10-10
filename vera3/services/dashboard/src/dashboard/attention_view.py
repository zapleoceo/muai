"""Панель «Внимание владельца» на `/tasks`: состояние, флаг, очередь отложенного."""
from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError
from vera_shared.owner_attention import store
from vera_shared.owner_attention.policy import (
    count_cross_midnight,
    hhmm,
    quiet_enabled,
    window_end,
)

from dashboard.render import esc

log = logging.getLogger(__name__)
OFF_TEXT = "Тихие часы выключены"
BROKEN_TEXT = "Панель недоступна"
STATE_TONE = {store.ACTIVE: "ok", store.WAIT10: "warn", store.QUIET: "off"}


def state_label(state: store.Attention) -> str:
    if state.state == store.WAIT10 and state.wait_deadline:
        return f"wait10 (до {hhmm(state.wait_deadline)})"
    if state.state == store.QUIET and state.quiet_until:
        return f"quiet до {hhmm(state.quiet_until)}"
    return state.state


Loaded = tuple[store.Attention, list[store.Deferred], list[store.QuestionOut]]


async def load_attention() -> Loaded | None:
    """None - тихий режим выключен или таблиц нет: страница всё равно рисуется."""
    if not quiet_enabled():
        return None
    try:
        return (await store.load_state(), await store.pending(),
                await store.undelivered_questions())
    except SQLAlchemyError:
        log.warning("owner-attention: таблицы недоступны (миграция 054 не применена?)",
                    exc_info=True)
        return None


def settings_block() -> str:
    cross = "вкл" if count_cross_midnight() else "выкл"
    end = window_end()
    end_text = f"{end:%H:%M}" if end else "не задано (нет верхней границы)"
    return ('<ul class="muted">'
            f'<li>OWNER_QUIET_COUNT_CROSS_MIDNIGHT: {cross} · не утверждено</li>'
            f'<li>OWNER_QUIET_WINDOW_END: {end_text} · не утверждено</li></ul>')


def attention_panel(state: store.Attention, deferred: list[store.Deferred],
                    enabled: bool | None = None,
                    undelivered: list[store.QuestionOut] | None = None) -> str:
    on = quiet_enabled() if enabled is None else enabled
    flag = (f'<span class="pill {"ok" if on else "off"}">'
            f'OWNER_QUIET_ENABLED: {"вкл" if on else "выкл"}</span>')
    items = "".join(
        f'<li><span class="muted">{esc(i.kind)} · {esc(hhmm(i.created_at))}</span> {esc(i.text)}</li>'
        for i in deferred) or '<li class="muted">Очередь отложенного пуста</li>'
    waiting = "".join(
        f'<li><span class="pill warn">ожидает доставки</span> {esc(q.task_id)}: {esc(q.question)}</li>'
        for q in undelivered or [])
    waiting_html = f'<h3>Ожидают доставки ({len(undelivered or [])})</h3><ul>{waiting}</ul>' if waiting else ""
    return (f'<section class="tk-att"><h2>Внимание владельца</h2>'
            f'<div class="tk-meta"><span class="pill {STATE_TONE.get(state.state, "off")}">'
            f'{esc(state_label(state))}</span>{flag}'
            f'<span class="muted">время Asia/Ho_Chi_Minh</span></div>'
            f'{waiting_html}<h3>Отложено ({len(deferred)})</h3><ul>{items}</ul>{settings_block()}</section>')


def attention_notice(text: str) -> str:
    return (f'<section class="tk-att"><h2>Внимание владельца</h2>'
            f'<div class="tk-meta"><span class="pill off">{esc(text)}</span></div></section>')


async def attention_html() -> str:
    """Панель для `/tasks`: флаг выкл - таблиц не трогаем; сбой БД - заглушка."""
    if not quiet_enabled():
        return attention_notice(OFF_TEXT)
    loaded = await load_attention()
    return attention_panel(loaded[0], loaded[1], undelivered=loaded[2]) if loaded else attention_notice(BROKEN_TEXT)
