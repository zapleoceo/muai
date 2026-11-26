"""HTML админки хаба: проекты и апстримы. Секрет в форме — только поле на запись."""
from __future__ import annotations

from typing import Any

from vera_shared.db.models_hub import HubUpstreamRow, upstream_dict
from vera_shared.hub.names import PROJECTS
from vera_shared.hub.spec import HTTP_AUTH, STDIO_AUTH

from dashboard.render import data_table, esc, local_dt

HUB_PUBLIC_URL = "https://dima.veranda.my/hub/mcp"
_PILL = {"ok": "ok", "down": "err", "needs_secret": "warn", "needs_config": "warn",
         "disabled": "", "unknown": ""}
_LABEL = {"ok": "работает", "down": "недоступен", "needs_secret": "ждёт секрета",
          "needs_config": "нет настроек", "disabled": "выключен", "unknown": "не проверен"}


def health_pill(health: str) -> str:
    return f'<span class="pill {_PILL.get(health, "")}">{esc(_LABEL.get(health, health))}</span>'


def _options(values: tuple[str, ...] | list[str], current: str) -> str:
    return "".join(f'<option value="{esc(v)}"{" selected" if v == current else ""}>{esc(v)}</option>'
                   for v in values)


def upstream_form(up: dict[str, Any] | None) -> str:
    u = up or {"id": "", "project": "vera", "slug": "", "title": "", "transport": "http",
               "url": "", "command": [], "auth_kind": "none", "auth_header": "",
               "has_secret": False, "enabled": True, "hint": "", "timeout_s": 30,
               "max_concurrency": 4}
    projects = "".join(f'<option value="{k}"{" selected" if k == u["project"] else ""}>{esc(v)}</option>'
                       for k, v in PROJECTS.items())
    secret_hint = "задан — пусто = оставить прежний" if u["has_secret"] else "не задан"
    return f"""<form method="post" action="/hub/upstream" class="grid" autocomplete="off">
<input type="hidden" name="id" value="{esc(u['id'])}">
<label>Проект <select name="project">{projects}</select></label>
<label>Слаг <input name="slug" value="{esc(u['slug'])}" pattern="[a-z][a-z0-9]{{1,15}}" required></label>
<label>Название <input name="title" value="{esc(u['title'])}"></label>
<label>Транспорт <select name="transport">{_options(("http", "stdio"), u['transport'])}</select></label>
<label>URL (http) <input name="url" value="{esc(u['url'])}" placeholder="https://…/mcp"></label>
<label>Команда (stdio, по аргументу в строке)
<textarea name="command" rows="2">{esc(chr(10).join(u['command']))}</textarea></label>
<label>Авторизация <select name="auth_kind">{_options(sorted(set(HTTP_AUTH) | set(STDIO_AUTH)), u['auth_kind'])}</select></label>
<label>Заголовок / env-переменная <input name="auth_header" value="{esc(u['auth_header'])}"></label>
<label>Секрет <input type="password" name="secret" value="" autocomplete="new-password"
 placeholder="{esc(secret_hint)}"></label>
<label><input type="checkbox" name="clear_secret"> стереть секрет</label>
<label><input type="checkbox" name="enabled"{" checked" if u['enabled'] else ""}> включён</label>
<label>Таймаут, с <input type="number" name="timeout_s" min="1" max="120" value="{esc(u['timeout_s'])}"></label>
<label>Параллельно <input type="number" name="max_concurrency" min="1" max="16" value="{esc(u['max_concurrency'])}"></label>
<label>Подсказка <textarea name="hint" rows="2">{esc(u['hint'])}</textarea></label>
<button type="submit">Сохранить</button></form>"""


def _upstream_row(up: dict[str, Any]) -> str:
    secret = ("—" if up["auth_kind"] == "none"
              else "задан" if up["has_secret"] else '<span class="warn">нет</span>')
    error = f'<div class="muted small">{esc(up["health_error"])}</div>' if up["health_error"] else ""
    hint = f'<div class="muted small">{esc(up["hint"])}</div>' if up["hint"] else ""
    where = up["url"] or " ".join(up["command"])
    return (f'<tr><td><code>{esc(up["project"])}__{esc(up["slug"])}__*</code>'
            f'<div>{esc(up["title"])}</div>{hint}</td>'
            f'<td>{esc(up["transport"])}<div class="muted small">{esc(where)}</div></td>'
            f'<td>{esc(up["auth_kind"])} / {secret}</td>'
            f'<td>{health_pill(up["health"])}{error}</td><td>{up["tool_count"]}</td>'
            f'<td>{local_dt(up["health_at"])}</td>'
            f'<td><details><summary>Изменить</summary>{upstream_form(up)}</details></td></tr>')


def upstreams_section(rows: list[HubUpstreamRow]) -> str:
    ups = [upstream_dict(r) for r in rows]
    parts = []
    for key, title in PROJECTS.items():
        mine = "".join(_upstream_row(u) for u in ups if u["project"] == key)
        parts.append(f"<h4>{esc(title)} <code class='muted small'>{key}</code></h4>"
                     + data_table(["Апстрим", "Транспорт", "Секрет", "Здоровье", "Инстр.",
                                   "Проверен", ""], mine, empty="нет апстримов"))
    return ("<h3>Проекты и апстримы</h3>" + "".join(parts)
            + f"<details><summary>Добавить апстрим</summary>{upstream_form(None)}</details>")


def hub_intro() -> str:
    return (f"<h2>MCP-хаб</h2><p>Один вход для агентов: <code>{HUB_PUBLIC_URL}</code>, "
            "заголовок <code>Authorization: Bearer &lt;токен агента&gt;</code>. Инструменты "
            "называются <code>проект__апстрим__инструмент</code>; агент видит только "
            "разрешённое ему. Секреты апстримов хранятся зашифрованными и после сохранения "
            "не показываются. HeyGen в хаб не подключается — правила HeyGen запрещают "
            "MCP-вход через шлюз (docs/mcp-hub.md).</p>")
