"""Действие Trello → человекочитаемая строка. Чистые функции, без сети и БД.

Отдельно от mapper.py, потому что это язык, а не контракт: набор типов
действий Trello будет расти, и трогать при этом упаковку события незачем.
"""
from __future__ import annotations

from typing import Any

# Что вообще считаем событием. Остальное (pos, idAttachmentCover, подписки на
# карточку) — служебный шум Trello, в память не идёт.
SUPPORTED_TYPES = frozenset({
    "createCard",
    "updateCard",
    "deleteCard",
    "commentCard",
    "addMemberToCard",
    "removeMemberFromCard",
    "updateCheckItemStateOnCard",
    "addChecklistToCard",
    "moveCardToBoard",
})

CATEGORY_BY_TYPE = {
    "createCard": "card",
    "updateCard": "card",
    "deleteCard": "card",
    "moveCardToBoard": "card",
    "commentCard": "comment",
    "addMemberToCard": "member",
    "removeMemberFromCard": "member",
    "updateCheckItemStateOnCard": "checklist",
    "addChecklistToCard": "checklist",
}


def _card_name(data: dict[str, Any]) -> str:
    return str((data.get("card") or {}).get("name") or "без названия")


def _member_name(data: dict[str, Any]) -> str:
    m = data.get("member") or {}
    return str(m.get("fullName") or m.get("username") or "участник")


def _due(value: Any) -> str:
    """ISO-срок Trello в короткий вид: 2026-08-30T12:00:00.000Z → 2026-08-30 12:00."""
    text = str(value or "")
    if len(text) >= 16 and text[10] == "T":
        return f"{text[:10]} {text[11:16]}"
    return text


def _describe_update(data: dict[str, Any]) -> str | None:
    """updateCard — одно имя на десяток разных правок, разбираем по data.old."""
    card = _card_name(data)
    old = data.get("old") or {}
    new = data.get("card") or {}

    if "idList" in old:
        before = (data.get("listBefore") or {}).get("name") or "?"
        after = (data.get("listAfter") or {}).get("name") or "?"
        return f'перенёс карточку «{card}»: {before} → {after}'
    if "due" in old:
        if new.get("due"):
            return f'поставил срок по карточке «{card}»: {_due(new["due"])}'
        return f'снял срок с карточки «{card}»'
    if "dueComplete" in old:
        state = "выполнено" if new.get("dueComplete") else "не выполнено"
        return f'отметил срок по карточке «{card}» как {state}'
    if "closed" in old:
        return (f'заархивировал карточку «{card}»' if new.get("closed")
                else f'вернул из архива карточку «{card}»')
    if "name" in old:
        return f'переименовал карточку: «{old["name"]}» → «{card}»'
    if "desc" in old:
        return f'изменил описание карточки «{card}»'
    return None


def describe(action: dict[str, Any]) -> str | None:
    """Описание действия, либо None — если событие того не стоит."""
    kind = str(action.get("type") or "")
    if kind not in SUPPORTED_TYPES:
        return None
    data = action.get("data") or {}
    card = _card_name(data)

    if kind == "createCard":
        where = (data.get("list") or {}).get("name")
        return f'создал карточку «{card}»' + (f' в списке «{where}»' if where else "")
    if kind == "updateCard":
        return _describe_update(data)
    if kind == "deleteCard":
        return f'удалил карточку «{card}»'
    if kind == "moveCardToBoard":
        src = (data.get("boardSource") or {}).get("name") or "?"
        return f'перенёс карточку «{card}» с доски «{src}»'
    if kind == "commentCard":
        text = str(data.get("text") or "").strip()
        return f'комментарий к карточке «{card}»:\n{text}' if text else None
    if kind == "addMemberToCard":
        return f'назначил {_member_name(data)} на карточку «{card}»'
    if kind == "removeMemberFromCard":
        return f'снял {_member_name(data)} с карточки «{card}»'
    if kind == "updateCheckItemStateOnCard":
        item = (data.get("checkItem") or {}).get("name") or "пункт"
        done = (data.get("checkItem") or {}).get("state") == "complete"
        mark = "выполнил" if done else "снял отметку с"
        return f'{mark} пункт «{item}» в карточке «{card}»'
    if kind == "addChecklistToCard":
        name = (data.get("checklist") or {}).get("name") or "чек-лист"
        return f'добавил чек-лист «{name}» в карточку «{card}»'
    return None


def category(action_type: str) -> str:
    return CATEGORY_BY_TYPE.get(action_type, "card")
