"""Единый набор значков источников: линейные SVG одной толщины вместо смеси эмодзи.

Цвет берётся из текста (`currentColor`), размер — из шрифта. Незнакомому источнику
достаётся нейтральная точка: значок обязателен, потому что по нему строка находится глазами."""
from __future__ import annotations

_PATHS: dict[str, str] = {
    "telegram": '<path d="M22 2 11 13"/><path d="M22 2l-7 20-4-9-9-4z"/>',
    "gmail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>',
    "slack": '<path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18"/>',
    "instagram": '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3z"/>'
                 '<circle cx="12" cy="13" r="3"/>',
    "trello": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M15 3v18"/>',
    "voice": '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 19v3"/>',
    "vera_chat": '<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.2A8.4 8.4 0 1 1 21 11.5z"/>',
    "vera_memory": '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>',
    "claude": '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>',
    "claude_chat": '<path d="m4 17 6-6-6-6M12 19h8"/>',
    "perplexity": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
}
_DEFAULT = '<circle cx="12" cy="12" r="4"/>'


def source_icon(key: str) -> str:
    return ('<svg class="ico-svg" viewBox="0 0 24 24" aria-hidden="true">'
            f'{_PATHS.get(key, _DEFAULT)}</svg>')
