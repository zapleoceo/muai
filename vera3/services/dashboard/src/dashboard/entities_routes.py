"""Entity-dedup review pages (`/entities/duplicates`, `/entities/merge`) +
avatar serving (`/entities/{id}/avatar`)."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from vera_shared.graph.avatars import get_avatar
from vera_shared.graph.dedup import (
    find_alias_collisions,
    find_duplicates_by_name,
    get_entity_context,
    merge_entities,
)

from dashboard.render import (
    _render,
    esc,
    initials_avatar_svg,
    owner_or_auth_error,
    tg_link,
)

router = APIRouter()


def _entity_cell(entity_id: int, name: str, username: str | None,
                 tg_id: int | str | None) -> str:
    """Avatar + name + (optional) clickable Telegram link — shared by both
    dedup sections."""
    link = tg_link(username, tg_id)
    name_html = esc(name)
    if link:
        target = ' target="_blank" rel="noopener"' if link.startswith("http") else ""
        name_html = f'<a href="{esc(link)}"{target}>{esc(name)}</a>'
    handle = f' <span class="mute">@{esc(username)}</span>' if username else ""
    return (
        f'<img src="/entities/{entity_id}/avatar" width="28" height="28" '
        f'style="border-radius:50%;vertical-align:middle;margin-right:6px" '
        f'alt="" loading="lazy">{name_html}{handle}'
    )


@router.get("/entities/{entity_id}/avatar")
async def entity_avatar(entity_id: int, request: Request):
    """Serve the entity's profile photo, or a deterministic initials SVG when
    none is stored yet. Owner-gated like every dashboard route."""
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    got = await get_avatar(entity_id)
    if got is not None:
        image, mime = got
        return Response(content=image, media_type=mime,
                        headers={"Cache-Control": "public, max-age=86400"})
    ctx = await get_entity_context(entity_id)
    svg = initials_avatar_svg(ctx.get("name"), seed=entity_id)
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


def _collision_section(groups: list[dict]) -> str:
    if not groups:
        return ""
    blocks = []
    for g in groups:
        cands = g["candidates"]
        cand_opts = "".join(
            f'<option value="{c["id"]}">#{c["id"]} {esc(c["name"])} ({esc(c["type"])})</option>'
            for c in cands
        )
        rows = "".join(
            f'<tr><td>#{c["id"]}</td>'
            f'<td>{_entity_cell(c["id"], c["name"], g["username"], None)}</td>'
            f'<td>{esc(c["type"])}</td></tr>'
            for c in cands
        )
        blocks.append(
            f'<div class="dup-group" style="border:1px solid #2f9e44;'
            f'padding:10px;margin:10px 0;border-radius:6px">'
            f'<b>@{esc(g["username"])}</b> — {g["size"]} сущности с этим @username'
            f'<table style="width:100%;margin-top:6px;font-size:13px">'
            f'<thead><tr><th>id</th><th>сущность</th><th>тип</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            f'<form method="post" action="/entities/merge" style="margin-top:6px">'
            f'  keeper: <select name="keeper_id">{cand_opts}</select>'
            f'  merged: <select name="merged_id">{cand_opts}</select>'
            f'  <button>merge</button></form>'
            f'</div>'
        )
    return (
        '<div class="section" style="border-left:3px solid #2f9e44">'
        '<h2>🎯 Точные совпадения по @username</h2>'
        '<p class="mute">Разные entity-строки с ОДНИМ @username — почти всегда '
        'один и тот же объект Telegram (напр. канал, который постит от своего '
        'имени, породил и channel-, и person-сущность). Это настоящие дубли — '
        'смело объединяй.</p>'
        f'{"".join(blocks)}</div>'
    )


@router.get("/entities/duplicates", response_class=HTMLResponse)
async def entity_duplicates_page(request: Request):
    if (resp := owner_or_auth_error(request)) is not None:
        return resp

    collisions = await find_alias_collisions(min_group=2)
    collision_html = _collision_section(collisions)

    groups = await find_duplicates_by_name(min_group=2)
    rows_html = []
    for g in groups[:50]:    # top-50 to keep page bounded
        cands = g["candidates"]
        sub = []
        contexts = {}
        for c in cands:
            ctx = await get_entity_context(c["id"])
            contexts[c["id"]] = ctx
            cell = _entity_cell(c["id"], c["name"], ctx.get("username"),
                                ctx.get("tg_id"))
            sub.append(
                f'<tr><td>#{c["id"]}</td><td>{cell}</td>'
                f'<td>{len(ctx["aliases"])}</td>'
                f'<td>{ctx["recent_30d_messages"]}</td>'
                f'<td>{len(ctx["memberships"])}</td></tr>'
            )
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
      {collision_html}
      <h2>👥 Кандидаты на объединение по имени</h2>
      <p class="mute">Группы entity-строк с одинаковым нормализованным именем.
      Выбери «keeper» и «merged» — после кнопки merge все aliases / memberships /
      relationships переедут на keeper, дубль удалится.</p>
      <div class="section" style="border-left:3px solid #f59f00">
        <b>⚠️ Почти всегда это РАЗНЫЕ люди:</b> Каждая группа здесь — N разных
        Telegram-аккаунтов с одинаковым first_name (напр. 21 «Дима» = 21 разный
        человек, у каждого свой @username и tg_id). Кликай по имени/аватарке,
        чтобы проверить в Telegram, и объединяй ТОЛЬКО настоящие дубли.
        Автоматические — в зелёной секции выше.
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
