"""Кнопки владельца в карточке `/office/hourly`: продолжить, ждать, пауза, отменить.

Формы шлют POST на существующие маршруты `/tasks/...` (ворота владельца и same-origin там).
Скрипт страницы перехватывает отправку, чтобы не уходить со страницы, и после ответа
перечитывает карточку; закрытие окна после действия перезагружает доску.
"""
from __future__ import annotations

from urllib.parse import quote

from vera_shared.room.attention import PAUSED
from vera_shared.room.stall_rules import SNOOZE, days_word

from dashboard.render import esc

FINAL = ("done", "cancelled")


def _form(url: str, label: str, reason: str, confirm: str = "") -> str:
    ask = f' data-confirm="{esc(confirm)}"' if confirm else ""
    return (f'<form class="kb-act" method="post" action="{esc(url)}" data-owner-action{ask}>'
            f'<input type="hidden" name="reason" value="{esc(reason)}">'
            f'<button type="submit">{esc(label)}</button></form>')


def owner_actions_bar(item) -> str:
    row = item.row
    if row.status in FINAL:
        return ""
    base = f"/tasks/{quote(row.room, safe='')}/{quote(row.task_id, safe='')}"
    paused = getattr(getattr(item, "attention", None), "state", None) == PAUSED
    snooze = f"Ждать {SNOOZE.days} {days_word(SNOOZE.days)}"
    pause = (_form(f"{base}/resume", "Снять паузу", "Снять паузу: кнопка страницы офиса")
             if paused else _form(f"{base}/pause", "Пауза", "Пауза: кнопка страницы офиса"))
    return ('<div class="kb-actions" role="group" aria-label="Действия владельца">'
            + _form(f"{base}/move/continue", "Продолжить", "Продолжить: кнопка страницы офиса")
            + _form(f"{base}/move/snooze", snooze, "Ждать: кнопка страницы офиса")
            + pause
            + _form(f"{base}/move/cancel", "Отменить", "Отмена: кнопка страницы офиса",
                    confirm="Отменить задачу насовсем? Взять её снова будет нельзя.")
            + '<p class="kb-act-result" aria-live="polite"></p></div>')


ACTIONS_CSS = """<style>
.kb-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 16px}
.kb-act{margin:0}.kb-act button{padding:6px 12px;border-radius:8px;border:1px solid var(--line,#ccc);background:var(--panel,#fff);color:inherit;cursor:pointer}
.kb-act button:hover{border-color:var(--accent,#888)}.kb-act-result{flex-basis:100%;margin:4px 0 0;font-size:.9em}
</style>"""

ACTIONS_JS = """
let kbChanged=false;
content.addEventListener('submit',async(event)=>{const form=event.target.closest('form[data-owner-action]');if(!form)return;event.preventDefault();
if(form.dataset.confirm&&!window.confirm(form.dataset.confirm))return;
const out=content.querySelector('.kb-act-result');const btn=form.querySelector('button');btn.disabled=true;if(out)out.textContent='Выполняю…';
try{const response=await fetch(form.action,{method:'POST',body:new FormData(form),credentials:'same-origin'});
if(!response.ok){const text=(await response.text()).replace(/<[^>]*>/g,'').trim();throw new Error(text||('Ошибка '+response.status))}
kbChanged=true;const detail=await fetch(trigger.dataset.detail,{credentials:'same-origin'});content.innerHTML=await detail.text();
const done=content.querySelector('.kb-act-result');if(done)done.textContent='Готово'}
catch(error){if(out)out.textContent=error.message;btn.disabled=false}});
dialog.addEventListener('close',()=>{if(kbChanged)location.reload()});
"""
