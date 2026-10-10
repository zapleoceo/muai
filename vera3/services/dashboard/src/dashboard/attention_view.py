"""Панель «Внимание владельца» на `/tasks`: состояние, флаг, очередь отложенного."""
from __future__ import annotations

from vera_shared.owner_attention import store
from vera_shared.owner_attention.policy import hhmm, quiet_enabled

from dashboard.render import esc

STATE_TONE = {store.ACTIVE: "ok", store.WAIT10: "warn", store.QUIET: "off"}


def state_label(state: store.Attention) -> str:
    if state.state == store.WAIT10 and state.wait_deadline:
        return f"wait10 (до {hhmm(state.wait_deadline)})"
    if state.state == store.QUIET and state.quiet_until:
        return f"quiet до {hhmm(state.quiet_until)}"
    return state.state


async def load_attention() -> tuple[store.Attention, list[store.Deferred]]:
    return await store.load_state(), await store.pending()


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
