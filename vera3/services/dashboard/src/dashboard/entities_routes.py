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
    get_entity_dossiers,
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


_PROJECT_COLOR = {"itstep": "#4dabf7", "veranda": "#69db7c", "family": "#f783ac",
                  "personal": "#ffa94d", "news": "#868e96", "other": "#495057"}


def _candidate_card(d: dict) -> str:
    """Rich per-entity context so the owner can tell WHO this is: avatar+link,
    dominant project, top chats, and a couple of real message snippets."""
    head = _entity_cell(d["entity_id"], d["name"] or "—", d.get("username"),
                        d.get("tg_id"))
    proj = d.get("dom_project")
    proj_chip = (
        f'<span style="background:{_PROJECT_COLOR.get(proj, "#495057")};color:#0f1115;'
        f'padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600">'
        f'{esc(proj)}</span>' if proj else ""
    )
    chats = " · ".join(
        f'{esc((c or "—")[:28])} <span class="mute">({n})</span>'
        for c, n in d.get("top_chats", [])
    ) or '<span class="mute">нет сообщений</span>'
    samples = "".join(
        f'<div class="mute" style="font-size:12px;margin-top:3px;'
        f'border-left:2px solid #2a2d34;padding-left:6px">«{esc(s)}»</div>'
        for s in d.get("samples", [])
    )
    return (
        f'<div style="padding:8px 0;border-bottom:1px solid #2a2d34">'
        f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
        f'#{d["entity_id"]} {head} {proj_chip} '
        f'<span class="mute" style="font-size:11px">{d.get("msg_count", 0)} сообщ.</span></div>'
        f'<div style="font-size:12px;margin-top:4px">💬 {chats}</div>'
        f'{samples}</div>'
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


def _collision_section(groups: list[dict], dossiers: dict[int, dict]) -> str:
    if not groups:
        return ""
    blocks = []
    for g in groups:
        cands = g["candidates"]
        cand_opts = "".join(
            f'<option value="{c["id"]}">#{c["id"]} {esc(c["name"])} ({esc(c["type"])})</option>'
            for c in cands
        )
        cards = "".join(_candidate_card(dossiers[c["id"]]) for c in cands)
        blocks.append(
            f'<div class="dup-group" style="border:1px solid #2f9e44;'
            f'padding:10px;margin:10px 0;border-radius:6px">'
            f'<b>@{esc(g["username"])}</b> — {g["size"]} сущности с этим @username'
            f'{cards}'
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


_NAME_GROUPS_SHOWN = 25
_CANDS_PER_GROUP = 6


@router.get("/entities/duplicates", response_class=HTMLResponse)
async def entity_duplicates_page(request: Request):
    if (resp := owner_or_auth_error(request)) is not None:
        return resp

    collisions = await find_alias_collisions(min_group=2)
    groups = await find_duplicates_by_name(min_group=2)
    shown_groups = groups[:_NAME_GROUPS_SHOWN]

    # Every candidate's dossier in one batched call (4 queries, one session) —
    # per-entity fanout would exhaust the connection pool on ~200 candidates.
    need_ids: set[int] = {c["id"] for g in collisions for c in g["candidates"]}
    for g in shown_groups:
        for c in g["candidates"][:_CANDS_PER_GROUP]:
            need_ids.add(c["id"])
    dossiers = await get_entity_dossiers(list(need_ids))

    collision_html = _collision_section(collisions, dossiers)

    rows_html = []
    for g in shown_groups:
        cands = g["candidates"][:_CANDS_PER_GROUP]
        extra = g["size"] - len(cands)
        cards = "".join(_candidate_card(dossiers[c["id"]]) for c in cands)
        cand_options = "".join(
            f'<option value="{c["id"]}">#{c["id"]} {esc(c["name"])} '
            f'({dossiers[c["id"]].get("dom_project") or "?"})</option>'
            for c in cands
        )
        more = (f'<div class="mute" style="font-size:12px">…и ещё {extra} '
                f'(сузь фильтром в Telegram)</div>' if extra > 0 else "")
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
            f'<b>«{esc(g["normalized"])}»</b> — {g["size"]} кандидатов'
            f'{cards}{more}{merge_form}'
            f'</div>'
        )

    return HTMLResponse(_render("entities", f"""
      {collision_html}
      <h2>👥 Кандидаты на объединение по имени</h2>
      <p class="mute">Группы entity-строк с одинаковым нормализованным именем.
      У каждой — проект, топ-чаты и примеры сообщений, чтобы понять, КТО это.
      Выбери «keeper» и «merged» — при merge все aliases / memberships /
      relationships переедут на keeper, дубль удалится.</p>
      <div class="section" style="border-left:3px solid #f59f00">
        <b>⚠️ Почти всегда это РАЗНЫЕ люди:</b> Каждая группа — N разных
        Telegram-аккаунтов с одинаковым first_name (напр. 21 «Дима» = 21 разный
        человек, свой @username и tg_id). Контекст ниже (чаты/сообщения) помогает
        отличить; объединяй ТОЛЬКО настоящие дубли. Автоматические — зелёная
        секция выше. Кросс-именной анализ Веры (Маша=Matia) — следующим шагом.
      </div>
      <p class="mute">Найдено групп: <b>{len(groups)}</b> (показано {len(shown_groups)}).</p>
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
