"""Разбивки по одному источнику. Провайдеры отдают ДАННЫЕ, не разметку.

Блок — это `{"title", "kind", …}`:

* `kind="rows"` — пары «подпись / значение» (`pairs`);
* `kind="table"` — `headers` + `rows`.

У блока может быть `hint` — строка под ним, где объясняется, что значат
значения. Рисует всё это один общий код в `sources_routes`, поэтому маршрут не
знает ни одного имени источника, а новый источник добавляет здесь одну функцию
(или не добавляет вовсе — тогда на странице будут состояние и объём).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import (
    GmailAccountRow,
    InstagramSessionRow,
    SlackAuthRow,
    SlackConversationRow,
    SlackThreadRow,
    TelegramSessionRow,
    TrelloBoardRow,
)

from dashboard.render import local_dt

Block = dict[str, Any]


class Html(str):
    """Готовая разметка — пилюля состояния, `<time>`. Экранировать её нельзя.

    Всё, что провайдер отдаёт обычной строкой, страница экранирует сама. Это
    важнее, чем кажется: chat_title, имя канала и username приходят из БД и
    полностью подконтрольны чужому человеку — любой может назвать чат
    `<script>…</script>`. Правило «по умолчанию экранируем, разметку помечаем
    явно» ошибиться не даёт, а обратное — «провайдер сам не забудет» — даёт.
    """


def dt(value, fmt: str = "datetime", empty: str = "—") -> Html:
    return Html(local_dt(value, fmt, empty))


def rows_block(title: str, pairs: list[tuple[str, str]], hint: str = "") -> Block:
    return {"title": title, "kind": "rows", "pairs": pairs, "hint": hint}


def table_block(title: str, headers: list[str], rows: list[list[str]],
                hint: str = "", empty: str = "нет данных") -> Block:
    return {"title": title, "kind": "table", "headers": headers, "rows": rows,
            "hint": hint, "empty": empty}


def state_pill(ok: bool, ok_text: str = "подключено",
               bad_text: str = "не подключено") -> Html:
    cls, label = ("ok", ok_text) if ok else ("err", bad_text)
    return Html(f'<span class="pill {cls}">{label}</span>')


async def _group(sql: str, **params: Any) -> list[tuple]:
    async with get_session() as s:
        return list((await s.execute(text(sql), params)).all())


def _counts(pairs: list[tuple], hint: str = "", title: str = "") -> Block:
    return rows_block(title, [(str(a if a is not None else "—"), f"{b:,}")
                              for a, b in pairs], hint)


# ─── telegram ───────────────────────────────────────────────────────────────


async def _telegram() -> list[Block]:
    async with get_session() as s:
        sessions = (await s.execute(
            select(TelegramSessionRow).order_by(TelegramSessionRow.id))).scalars().all()
    by_type = await _group(
        "SELECT COALESCE(metadata->>'chat_type', category), COUNT(*) "
        "FROM events WHERE source='telegram' GROUP BY 1 ORDER BY 2 DESC")
    by_dir = await _group(
        "SELECT COALESCE(metadata->>'direction','?'), COUNT(*) "
        "FROM events WHERE source='telegram' GROUP BY 1 ORDER BY 2 DESC")
    top = await _group(
        "SELECT COALESCE(metadata->>'chat_title','(без названия)'), "
        "COALESCE(metadata->>'chat_type','?'), COUNT(*) "
        "FROM events WHERE source='telegram' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20")
    return [
        table_block("Сессия userbot", ["телефон", "состояние", "заведена"],
                    [[r.phone, state_pill(r.is_active, "активна", "неактивна"),
                      dt(r.created_at, "date")] for r in sessions],
                    empty="сессии нет — жми «Переподключить»"),
        _counts(by_type, "user — личка · chat — малая группа · channel — канал "
                         "или супергруппа", "По типу чата"),
        _counts(by_dir, "received — входящие · sent — исходящие", "По направлению"),
        table_block("Топ-20 чатов по объёму", ["чат", "тип", "событий"],
                    [[t or "—", k or "?", f"{c:,}"] for t, k, c in top]),
    ]


# ─── gmail ──────────────────────────────────────────────────────────────────


async def _gmail() -> list[Block]:
    async with get_session() as s:
        accounts = (await s.execute(
            select(GmailAccountRow).order_by(GmailAccountRow.id))).scalars().all()
    per_account = dict(await _group(
        "SELECT account, COUNT(*) FROM events WHERE source='gmail' GROUP BY 1"))

    rows = []
    for a in accounts:
        if a.needs_reauth:
            state = Html('<span class="pill err">токен отозван</span>')
        elif not a.is_active:
            state = Html('<span class="pill err">выключен</span>')
        else:
            state = Html('<span class="pill ok">живой</span>')
        rows.append([a.email, state, dt(a.last_polled_at, "datetime", "никогда"),
                     f"{per_account.get(a.email, 0):,}",
                     (a.last_error or "")[:120] or "—"])
    return [
        table_block("Ящики", ["адрес", "состояние", "последний опрос",
                              "событий", "последняя ошибка"], rows,
                    hint="Google отзывает токен, если приложение OAuth стоит в "
                         "режиме Testing и неделю не используется.",
                    empty="ящиков нет"),
    ]


# ─── slack ──────────────────────────────────────────────────────────────────


async def _slack() -> list[Block]:
    async with get_session() as s:
        auth = (await s.execute(
            select(SlackAuthRow).order_by(SlackAuthRow.id))).scalars().all()
        conversations = (await s.execute(
            select(SlackConversationRow)
            .where(SlackConversationRow.is_active.is_(True))
            .order_by(SlackConversationRow.name))).scalars().all()
        threads = (await s.execute(
            select(SlackThreadRow))).scalars().all()
    by_kind = await _group(
        "SELECT COALESCE(metadata->>'channel_kind','?'), COUNT(*) "
        "FROM events WHERE source='slack' GROUP BY 1 ORDER BY 2 DESC")
    by_thread = await _group(
        "SELECT CASE WHEN (metadata->>'in_thread')::text='true' "
        "THEN 'в тредах' ELSE 'в канале' END, COUNT(*) "
        "FROM events WHERE source='slack' GROUP BY 1 ORDER BY 2 DESC")
    top = await _group(
        "SELECT COALESCE(metadata->>'channel_name','(без названия)'), "
        "COALESCE(metadata->>'channel_kind','?'), COUNT(*) "
        "FROM events WHERE source='slack' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20")

    return [
        table_block("Подключение", ["воркспейс", "от чьего имени", "состояние",
                                    "последний успех", "последняя ошибка"],
                    [[a.team_name or a.team_id, a.username or a.user_id,
                      state_pill(a.is_active, "активен", "отозван"),
                      dt(a.last_ok_at, "datetime", "ещё не ходили"),
                      (a.last_error or "")[:120] or "—"] for a in auth],
                    hint="Токен пользовательский (xoxp-), хранится в БД под "
                         "шифрованием. Права видит только владелец токена.",
                    empty="токена нет — жми «Подключить»"),
        _counts(by_kind, "channel — канал · im — личка · mpim — групповое ЛС",
                "По типу канала"),
        _counts(by_thread,
                "Ответы в тредах приходят отдельным обходом: conversations.history "
                "отдаёт только корневое сообщение, а тред со старым корнем в "
                "истории не появляется вовсе.", "Канал против тредов"),
        rows_block("Обход", [
            ("Каналов под наблюдением", f"{len(conversations):,}"),
            ("Тредов под наблюдением", f"{len(threads):,}"),
        ]),
        table_block("Топ-20 каналов по объёму", ["канал", "тип", "событий"],
                    [[n or "—", k or "?", f"{c:,}"] for n, k, c in top]),
    ]


# ─── instagram ──────────────────────────────────────────────────────────────


async def _instagram() -> list[Block]:
    async with get_session() as s:
        sessions = (await s.execute(
            select(InstagramSessionRow).order_by(InstagramSessionRow.id))).scalars().all()
    by_dir = await _group(
        "SELECT COALESCE(metadata->>'direction','?'), COUNT(*) "
        "FROM events WHERE source='instagram' GROUP BY 1 ORDER BY 2 DESC")
    top = await _group(
        "SELECT COALESCE(metadata->>'thread_title','(без названия)'), "
        "COALESCE((metadata->>'is_group')::text,'false'), COUNT(*) "
        "FROM events WHERE source='instagram' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20")
    return [
        table_block("Сессия", ["аккаунт", "состояние", "последний опрос"],
                    [[f"@{r.username}", state_pill(r.is_active, "активна", "неактивна"),
                      dt(r.last_polled_at, "datetime_sec", "никогда")]
                     for r in sessions],
                    empty="сессии нет — жми «Подключить»"),
        _counts(by_dir, "received — входящие в ЛС · sent — исходящие",
                "По направлению"),
        table_block("Топ-20 диалогов", ["диалог", "вид", "событий"],
                    [[t or "—", "группа" if g == "true" else "личный", f"{c:,}"]
                     for t, g, c in top]),
    ]


# ─── trello ─────────────────────────────────────────────────────────────────


async def _trello() -> list[Block]:
    async with get_session() as s:
        boards = (await s.execute(
            select(TrelloBoardRow).order_by(TrelloBoardRow.name))).scalars().all()
    by_action = await _group(
        "SELECT COALESCE(metadata->>'action_type','?'), COUNT(*) "
        "FROM events WHERE source='trello' GROUP BY 1 ORDER BY 2 DESC LIMIT 15")
    return [
        table_block("Доски", ["доска", "состояние", "последний опрос", "ошибка"],
                    [[b.name or b.board_id,
                      state_pill(b.is_active, "открыта", "закрыта"),
                      dt(b.last_polled_at, "datetime", "никогда"),
                      (b.last_error or "")[:120] or "—"] for b in boards],
                    hint="Закрытая доска гаснет, а не удаляется: курсор переживёт "
                         "её возвращение.",
                    empty="досок нет — ключ Trello не задан"),
        _counts(by_action, "", "По типу действия"),
    ]


# ─── voice ──────────────────────────────────────────────────────────────────


async def _voice() -> list[Block]:
    agg = await _group("""
        SELECT COUNT(*) AS sessions,
               COALESCE(SUM((metadata->>'duration_s')::int), 0) AS total_s,
               COUNT(*) FILTER (WHERE (metadata->>'truncated')::text='true') AS cut,
               COUNT(*) FILTER (WHERE (metadata->>'distilled')::text='false') AS raw,
               COALESCE(MAX((metadata->>'windows')::int), 0) AS max_windows
        FROM events WHERE source='voice'
    """)
    sessions, total_s, cut, raw, max_windows = agg[0] if agg else (0, 0, 0, 0, 0)
    by_app = await _group(
        "SELECT COALESCE(metadata->>'app','(неизвестно)'), COUNT(*) "
        "FROM events WHERE source='voice' GROUP BY 1 ORDER BY 2 DESC LIMIT 15")
    return [
        rows_block("Разговоры", [
            ("Сессий", f"{sessions:,}"),
            ("Суммарно записано", f"{total_s // 3600} ч {total_s % 3600 // 60} мин"),
            ("Самая длинная — окон свёртки", str(max_windows)),
            ("Хвост обрезан", f"{cut:,}"),
            ("Не удалось осмыслить", f"{raw:,}"),
        ], hint="«Хвост обрезан» должен быть нулём: это аварийный потолок свёртки. "
                "Ненулевое значение значит, что часть разговора не осмыслена."),
        _counts(by_app, "", "Где говорили"),
    ]


PROVIDERS = {
    "telegram": _telegram,
    "gmail": _gmail,
    "slack": _slack,
    "instagram": _instagram,
    "trello": _trello,
    "voice": _voice,
}


async def blocks_for(key: str) -> list[Block]:
    provider = PROVIDERS.get(key)
    return await provider() if provider else []
