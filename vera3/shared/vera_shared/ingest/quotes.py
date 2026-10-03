"""Цитаты в письмах: отрезает историю ответа, оставляя то, что человек написал сам.

В карточке и «Входящем» сниппет письма иначе начинался бы с «> On 11 Aug … wrote:»
и выглядел так, будто автор — владелец. Узнаются заголовки ответа Gmail и Apple Mail
(английский и русский, в одну или две строки), Outlook («From/Sent» и
«-----Original Message-----») и блоки строк, начинающиеся с `>`.

Чего НЕ делаем: одиночная строка с `>` без блока — обычный текст (стрелка, сравнение), её
оставляем. Пересланное письмо («Forwarded message», «Begin forwarded message») — это содержимое,
а не история ответа: его тело сохраняется. Заголовок «From/Sent» без собственного текста
выше него — тоже пересылка без комментария, режем только настоящие цепочки ответа.
"""
from __future__ import annotations

import re

_WROTE_EN = re.compile(r"^\s*On\b.{4,300}\bwrote:\s*$", re.IGNORECASE)
_WROTE_RU = re.compile(r"^\s*.{0,300}\b(пишет|написал(?:а|и)?(?:\(а\))?)\s*:\s*$", re.IGNORECASE)
_ORIGINAL = re.compile(
    r"^\s*-{2,}\s*(Original Message|Исходное сообщение|Original-Nachricht)\s*-{2,}\s*$",
    re.IGNORECASE)
_FORWARD = re.compile(
    r"^\s*(-{2,}\s*(Forwarded message|Пересланное сообщение|Пересылаемое сообщение)\s*-{2,}"
    r"|Begin forwarded message:?)\s*$", re.IGNORECASE)
_RULE = re.compile(r"^\s*_{5,}\s*$")
_FROM = re.compile(r"^\s*(From|От|Von)\s*:", re.IGNORECASE)
_SENT = re.compile(r"^\s*(Sent|Date|Отправлено|Дата|Gesendet)\s*:", re.IGNORECASE)
_ADDRESS_OR_DATE = re.compile(r"[@\d]")
_LOOKAHEAD = 4
_MIN_QUOTE_BLOCK = 2


def _wrapped_wrote(lines: list[str], i: int) -> bool:
    """«On Mon, Aug 11 <a@b>» на одной строке и «wrote:» на следующей."""
    return (lines[i].strip().lower() == "wrote:" and i > 0
            and lines[i - 1].lstrip().lower().startswith("on "))


def _is_reply_header(lines: list[str], i: int) -> bool:
    line = lines[i]
    if _ORIGINAL.match(line) or _wrapped_wrote(lines, i):
        return True
    if (_WROTE_EN.match(line) or _WROTE_RU.match(line)) and _ADDRESS_OR_DATE.search(line):
        return True
    return _FROM.match(line) is not None and any(
        _SENT.match(x) for x in lines[i + 1:i + 1 + _LOOKAHEAD])


def _reply_start(lines: list[str]) -> int:
    for i in range(len(lines)):
        if _FORWARD.match(lines[i]):
            return len(lines)                 # дальше — пересланное содержимое
        if not _is_reply_header(lines, i):
            continue
        start = i - 1 if (_wrapped_wrote(lines, i) or (i and _RULE.match(lines[i - 1]))) else i
        if not any(ln.strip() for ln in lines[:start]):
            return len(lines)                 # своего текста выше нет: пересылка без комментария
        return start
    return len(lines)


def _drop_quote_blocks(lines: list[str]) -> list[str]:
    """Убирает блоки из двух и более подряд идущих строк-цитат; одиночное `>` остаётся."""
    kept: list[str] = []
    run: list[str] = []
    for ln in [*lines, ""]:
        if ln.lstrip().startswith(">"):
            run.append(ln)
            continue
        if len(run) < _MIN_QUOTE_BLOCK:
            kept.extend(run)
        run = []
        kept.append(ln)
    return kept[:-1]


def strip_quoted(text: str | None) -> str:
    """Текст без истории ответа и блоков-цитат; пустая строка, если всё — цитата."""
    lines = (text or "").splitlines()
    return "\n".join(_drop_quote_blocks(lines[:_reply_start(lines)])).strip()
