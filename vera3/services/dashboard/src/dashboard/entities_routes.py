"""Entity-dedup review pages (`/entities/duplicates`, `/entities/merge`)."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from vera_shared.graph.dedup import (
    find_duplicates_by_name,
    get_entity_context,
    merge_entities,
)

from dashboard.render import _render, esc, owner_or_auth_error

router = APIRouter()


@router.get("/entities/duplicates", response_class=HTMLResponse)
async def entity_duplicates_page(request: Request):
    if (resp := owner_or_auth_error(request)) is not None:
        return resp

    groups = await find_duplicates_by_name(min_group=2)
    rows_html = []
    for g in groups[:50]:    # top-50 to keep page bounded
        cands = g["candidates"]
        # Per-candidate sub-row with alias count + recent activity
        sub = []
        contexts = {}
        for c in cands:
            ctx = await get_entity_context(c["id"])
            contexts[c["id"]] = ctx
            sub.append(
                f'<tr><td>#{c["id"]}</td><td>{esc(c["name"])}</td>'
                f'<td>{len(ctx["aliases"])}</td>'
                f'<td>{ctx["recent_30d_messages"]}</td>'
                f'<td>{len(ctx["memberships"])}</td></tr>'
            )
        # Merge form: user picks one keeper + one to merge into it
        cand_options = "".join(
            f'<option value="{c["id"]}">#{c["id"]} {esc(c["name"])} '
            f'({contexts[c["id"]]["recent_30d_messages"]} recent)</option>'
            for c in cands
        )
        merge_form = (
            f'<form method="post" action="/entities/merge" style="margin-top:6px">'
            f'  keeper: <select name="keeper_id">{cand_options}</select>'
            f'  merged: <select name="merged_id">{cand_options}</select>'
            f'  <button>merge</button>'
            f'</form>'
        )
        rows_html.append(
            f'<div class="dup-group" style="border:1px solid #2a2d34;'
            f'padding:10px;margin:10px 0;border-radius:6px">'
            f'<b>«{esc(g["normalized"])}»</b> — {g["size"]} candidates'
            f'<table style="width:100%;margin-top:6px;font-size:13px">'
            f'<thead><tr><th>id</th><th>name</th><th>aliases</th>'
            f'<th>recent 30d msgs</th><th>memberships</th></tr></thead>'
            f'<tbody>{"".join(sub)}</tbody></table>'
            f'{merge_form}'
            f'</div>'
        )

    return HTMLResponse(_render("entities", f"""
      <h2>👥 Кандидаты на объединение</h2>
      <p class="mute">Группы entity-строк с одинаковым нормализованным именем.
      Выбери «keeper» и «merged» — после кнопки merge все aliases / memberships /
      relationships переедут на keeper, дубль удалится.</p>
      <div class="section" style="border-left:3px solid #f59f00">
        <b>⚠️ Авто-объединить нечего:</b> Каждая «дубль»-группа здесь — это
        N разных Telegram-аккаунтов с одинаковым first_name. Например 15
        «Alex» = 15 разных людей с TG user_id вида user:1919538618,
        user:1482567987 и т.д. (UNIQUE на sender_id предотвращает
        копирование).
        <br><br>
        Чтобы auto-merge сработал — нужен сильный сигнал: совпавший phone,
        совпавший @username, или эмбединг-сходство сообщений >0.85. Этого
        у нас на сегодня в данных НЕТ — все entity_aliases ведут на
        уникальные TG-id.
        <br><br>
        Реальные дубли (Дима имеет 2 TG-аккаунта, и т.п.) — определяются
        только тобой вручную через эту страницу.
      </div>
      <p class="mute">Найдено групп: <b>{len(groups)}</b> (показано {min(50,len(groups))}).</p>
      {''.join(rows_html) or '<p class="mute">Чисто — дублей по имени нет.</p>'}
    """))


@router.post("/entities/merge")
async def entity_merge(request: Request,
                       keeper_id: int = Form(...),  # noqa: B008
                       merged_id: int = Form(...)):  # noqa: B008
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    result = await merge_entities(keeper_id, merged_id)
    return RedirectResponse(
        f"/entities/duplicates?merged={result}",
        status_code=303,
    )
