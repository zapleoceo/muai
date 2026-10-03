"""Карточка события `/events/{id}`: заголовок ключ-значение, читаемое тело,
стенограмма в сворачиваемом блоке."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from dashboard.event_text import ParsedText, clean_name, describe, parse_content
from dashboard.render import esc, local_dt
from dashboard.source_registry import resolve_source
from dashboard.ui.components import collapsible, kv_block

#: Дорожка записи → кто говорил. Это и есть авторство на сегодня: своё
#: авторство доказано устройством, а не догадкой модели.
STREAM_LABEL: dict[str, str] = {"mic": "Я", "system": "собеседник"}

_HEADER_LABELS: tuple[tuple[str, str], ...] = (
    ("From", "От"), ("Author", "Автор"), ("To", "Кому"), ("Cc", "Копия"),
    ("Subject", "Тема"), ("Chat", "Чат"), ("Where", "Где"), ("Direction", "Направление"),
)
_DIRECTION = {"received": "входящее", "sent": "исходящее"}


def _utterance_row(u: Mapping[str, Any]) -> str:
    at = float(u.get("at") or 0.0)
    stamp = f"{int(at) // 60:02d}:{int(at) % 60:02d}"
    cls = "self" if u.get("stream") == "mic" else "other"
    # Имя, опознанное по голосу, вместо безликого «собеседник»: в созвоне
    # на пятерых иначе не видно, кто что сказал.
    who = (str(u.get("speaker")).strip() if u.get("speaker")
           else STREAM_LABEL.get(str(u.get("stream")), str(u.get("stream") or "?")))
    # Помеченное эхо — голос собеседника, пойманный микрофоном. Показать его
    # речью владельца значит соврать об авторстве, а спрятать — потерять слова:
    # в том же куске бывают и его собственные.
    if u.get("echo"):
        who, cls = f"{who} · эхо", "echo"
    return (f'<tr><td class="mute">{stamp}</td><td class="who {cls}">{esc(who)}</td>'
            f'<td>{esc(u.get("text") or "")}</td></tr>')


def _transcript_notes(utterances: list[Mapping[str, Any]]) -> str:
    echoes = sum(1 for u in utterances if u.get("echo"))
    voices = sorted({str(u["speaker"]).strip() for u in utterances if u.get("speaker")})
    voices_note = (
        f'<p class="mute">Голоса опознаны: {esc(", ".join(voices))}. Реплики с '
        f'одним именем сказал один человек. Имя берётся из заголовка окна в '
        f'разговоре один на один и дальше узнаётся по голосу; «Собеседник N» — '
        f'голос разделён, но назвать его неоткуда.</p>' if voices else "")
    echo_note = (
        f'<p class="mute">Помечено как эхо: {echoes}. Это речь собеседника, '
        f'пойманная микрофоном из динамиков. В выжимку такие реплики не '
        f'попадают, но здесь остаются — в одном куске с эхом бывают и слова '
        f'владельца.</p>' if echoes else "")
    return voices_note + echo_note


def transcript_html(extra: dict[str, Any] | None) -> str:
    """Стенограмма события → сворачиваемый HTML. Пусто, если её нет."""
    if not extra or extra.get("kind") != "voice_transcript":
        return ""
    utterances = extra.get("utterances") or []
    if not utterances:
        return ""
    rows = "".join(_utterance_row(u) for u in utterances)
    body = (
        '<p class="mute">Дословно, как распознал слушатель. В поиск по мозгу идёт '
        'только выжимка выше — иначе обрывки перебивали бы её. Звук не хранится '
        'вообще.</p>'
        f'{_transcript_notes(utterances)}'
        f'<div class="overflow-auto"><table class="data transcript"><tbody>{rows}</tbody></table></div>')
    title = f"Стенограмма ({len(utterances)} реплик, {extra.get('chars', 0)} символов)"
    return collapsible(title, body)


_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PEOPLE_KEYS = ("From", "Author", "To", "Cc")


def mail_addresses(parsed: ParsedText) -> list[str]:
    """Адреса из From/To/Cc: по ним маршрут ищет людей в графе."""
    found = {m.lower() for k in _PEOPLE_KEYS for m in _ADDRESS.findall(parsed.headers.get(k, ""))}
    return sorted(found)


def _people_html(value: str, links: Mapping[str, int]) -> str:
    """Список «Имя <адрес>, …»: известные графу люди — ссылкой на карточку в «Людях»."""
    parts = []
    for chunk in value.split(","):
        found = _ADDRESS.search(chunk)
        entity = links.get(found.group(0).lower()) if found else None
        entity = entity or links.get(clean_name(chunk).lower())
        text = esc(chunk.strip())
        parts.append(f'<a href="/graph#person={entity}">{text}</a>' if entity else text)
    return ", ".join(p for p in parts if p)


def header_pairs(parsed: ParsedText, who: str, links: Mapping[str, int] | None = None) -> list[tuple[str, str]]:
    """Поля заголовка в порядке чтения; пустые и дублирующие автора пропущены.
    «Кто» остаётся только там, где нет ни «От», ни «Автор» — иначе это тот же человек дважды."""
    pairs: list[tuple[str, str]] = []
    authored = any(clean_name(parsed.headers.get(k, "")) == who for k in ("From", "Author"))
    if who and not authored:
        pairs.append(("Кто", _people_html(who, links or {})))
    for key, label in _HEADER_LABELS:
        value = parsed.headers.get(key)
        if not value or (key == "Author" and "From" in parsed.headers):
            continue
        shown = _DIRECTION.get(value, value) if key == "Direction" else value
        pairs.append((label, _people_html(shown, links or {}) if key in _PEOPLE_KEYS else esc(shown)))
    return pairs


def service_pairs(row: Mapping[str, Any], meta: Mapping[str, Any]) -> list[tuple[str, str]]:
    src = resolve_source(row["source"] or "")
    where = " / ".join(str(meta[k]) for k in ("app", "window_title") if meta.get(k))
    return [
        ("источник", esc(src.title)),
        ("аккаунт", esc(row["account"] or "—")),
        ("вид", esc(row["category"] or "—")),
        ("важность", str(row["importance"]) if row["importance"] is not None else "—"),
        ("природа", esc(row["nature"] or "—")),
        ("проект", esc(row["project"] or "—")),
        ("разбор ИИ", esc(row["triage_status"] or "—")),
        ("где", esc(where or "—")),
    ]


def event_card(row: Mapping[str, Any], links: Mapping[str, int] | None = None) -> str:
    meta = row["metadata"] or {}
    parsed = parse_content(row["content_text"])
    who = describe(parsed, meta).who
    pairs = [*header_pairs(parsed, who, links), ("когда", local_dt(row["occurred_at"], "datetime"))]
    error = (f'<p class="err">Ошибка разбора: {esc(row["triage_error"])}</p>'
             if row["triage_error"] else "")
    body = esc(parsed.body) if parsed.body else '<span class="muted">текста нет</span>'
    return f"""
        <p class="crumb"><a href="/events">← во входящее</a></p>
        <h2>Событие {row['id']}</h2>
        {kv_block(pairs)}
        {error}
        <h3>{"Выжимка" if row["source"] == "voice" else "Текст"}</h3>
        <div class="body-text">{body}</div>
        {transcript_html(row['content_extra'])}
        {collapsible("Служебное", kv_block(service_pairs(row, meta)))}
    """
