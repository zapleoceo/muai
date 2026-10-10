"""Панель «Внимание владельца» на `/tasks`: состояние, флаг, очередь отложенного."""
from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError
from vera_shared.owner_attention import store
from vera_shared.owner_attention.policy import hhmm, quiet_enabled

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


async def load_attention() -> tuple[store.Attention, list[store.Deferred]] | None:
    """None - тихий режим выключен или таблиц нет: страница всё равно рисуется."""
    if not quiet_enabled():
        return None
    try:
        return await store.load_state(), await store.pending()
    except SQLAlchemyError:
        log.warning("owner-attention: таблицы недоступны (миграция 054 не применена?)",
                    exc_info=True)
        return None


def attention_panel(state: store.Attention, deferred: list[store.Deferred],
                    enabled: bool | None = None) -> str:
    on = quiet_enabled() if enabled is None else enabled
    flag = (f'<span class="pill {"ok" if on else "off"}">'
            f'OWNER_QUIET_ENABLED: {"вкл" if on else "выкл"}</span>')
    items = "".join(
        f'<li><span class="muted">{esc(i.kind)} · {esc(hhmm(i.created_at))}</span> {esc(i.text)}</li>'
        for i in deferred) or '<li class="muted">Очередь отложенного пуста</li>'
    return (f'<section class="tk-att"><h2>Внимание владельца</h2>'
            f'<div class="tk-meta"><span class="pill {STATE_TONE.get(state.state, "off")}">'
            f'{esc(state_label(state))}</span>{flag}'
            f'<span class="muted">время Asia/Ho_Chi_Minh</span></div>'
            f'<h3>Отложено ({len(deferred)})</h3><ul>{items}</ul></section>')


def attention_notice(text: str) -> str:
    return (f'<section class="tk-att"><h2>Внимание владельца</h2>'
            f'<div class="tk-meta"><span class="pill off">{esc(text)}</span></div></section>')


async def attention_html() -> str:
    """Панель для `/tasks`: флаг выкл - таблиц не трогаем; сбой БД - заглушка."""
    if not quiet_enabled():
        return attention_notice(OFF_TEXT)
    loaded = await load_attention()
    return attention_panel(*loaded) if loaded else attention_notice(BROKEN_TEXT)
