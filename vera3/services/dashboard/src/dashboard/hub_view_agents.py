"""HTML админки хаба: агенты, их права и журнал вызовов."""
from __future__ import annotations

from vera_shared.db.models_hub import HubAgentRow, HubCallRow, HubUpstreamRow
from vera_shared.hub.names import PROJECTS

from dashboard.render import data_table, esc, local_dt


def _grant_boxes(upstreams: list[HubUpstreamRow], allow: list[str]) -> str:
    grants = set(allow)
    out = []
    for key, title in PROJECTS.items():
        boxes = [f'<label><input type="checkbox" name="allow" value="{key}"'
                 f'{" checked" if key in grants else ""}> <b>{esc(title)}</b> (весь проект)</label>']
        for up in upstreams:
            if up.project != key:
                continue
            value = f"{key}/{up.slug}"
            boxes.append(f'<label><input type="checkbox" name="allow" value="{esc(value)}"'
                         f'{" checked" if value in grants else ""}> {esc(value)}</label>')
        out.append(f'<div class="grant-group">{"".join(boxes)}</div>')
    return "".join(out)


def agent_form(upstreams: list[HubUpstreamRow], agent: HubAgentRow | None = None) -> str:
    if agent is None:
        return (f'<form method="post" action="/hub/agent" class="grid" autocomplete="off">'
                f'<label>Имя <input name="name" pattern="[a-z][a-z0-9_-]{{1,31}}" required></label>'
                f'<label>Заметка <input name="note"></label>{_grant_boxes(upstreams, [])}'
                f'<button type="submit">Создать агента и выдать токен</button></form>')
    return (f'<form method="post" action="/hub/agent" class="grid">'
            f'<input type="hidden" name="id" value="{agent.id}">'
            f'<label><input type="checkbox" name="enabled"{" checked" if agent.enabled else ""}> включён</label>'
            f'<label>Заметка <input name="note" value="{esc(agent.note)}"></label>'
            f'{_grant_boxes(upstreams, list(agent.allow or []))}'
            f'<button type="submit">Сохранить права</button></form>'
            f'<form method="post" action="/hub/agent/rotate" '
            f'onsubmit="return confirm(\'Старый токен перестанет работать. Выпустить новый?\')">'
            f'<input type="hidden" name="id" value="{agent.id}">'
            f'<button type="submit" class="danger">Выпустить новый токен</button></form>')


def agents_section(agents: list[HubAgentRow], upstreams: list[HubUpstreamRow]) -> str:
    rows = "".join(
        f'<tr><td><b>{esc(a.name)}</b><div class="muted small">{esc(a.note)}</div></td>'
        f'<td><code>{esc(a.token_prefix)}…</code></td>'
        f'<td>{esc(", ".join(a.allow or [])) if a.allow else "<span class=muted>ничего</span>"}</td>'
        f'<td>{"да" if a.enabled else "<span class=err>нет</span>"}</td>'
        f'<td>{local_dt(a.last_seen_at)}</td>'
        f'<td><details><summary>Права</summary>{agent_form(upstreams, a)}</details></td></tr>'
        for a in agents)
    return ("<h3>Агенты</h3><p class='muted small'>У каждого агента свой токен; токен "
            "показывается один раз, в базе остаётся только его хеш.</p>"
            + data_table(["Агент", "Токен", "Права", "Вкл", "Был", ""], rows, empty="агентов нет")
            + f"<details><summary>Новый агент</summary>{agent_form(upstreams)}</details>")


def calls_section(calls: list[HubCallRow]) -> str:
    rows = "".join(
        f'<tr><td>{local_dt(c.created_at, "datetime_sec")}</td><td>{esc(c.agent)}</td>'
        f'<td>{esc(c.method)}</td><td><code>{esc(c.tool)}</code></td>'
        f'<td>{esc(c.status)}</td><td>{c.duration_ms}</td><td>{esc(c.error)}</td></tr>'
        for c in calls)
    return ("<h3>Журнал вызовов</h3><p class='muted small'>Только метаданные: без аргументов "
            "и ответов.</p>" + data_table(["Когда", "Агент", "Метод", "Инструмент", "Статус",
                                           "мс", "Ошибка"], rows, empty="вызовов не было"))


def token_page(name: str, token: str) -> str:
    return (f"<h2>Токен агента {esc(name)}</h2><p>Скопируйте сейчас — больше он не будет "
            "показан. Передайте агенту по защищённому каналу; в чат и в git не вставлять.</p>"
            f"<pre class='token'>{esc(token)}</pre><p><a href='/hub'>← к хабу</a></p>")
