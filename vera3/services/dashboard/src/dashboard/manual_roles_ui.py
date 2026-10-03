"""Подписи ролей для диалога «Указать связь»: ключи — из `vera_shared.graph.manual_roles`.

`{x}` — человек из открытой карточки, `{y}` — с кем связываем. Тексты читаются как
готовое утверждение: подтверждение показывает ровно то, что будет записано."""
from __future__ import annotations

import json

from vera_shared.graph.manual_roles import MANUAL_ROLES

ROLE_TEXT: dict[str, str] = {
    "x_boss_of_y": "{x} — начальник для {y}",
    "y_boss_of_x": "{y} — начальник для {x}",
    "coworkers": "{x} и {y} работают вместе",
    "friends": "{x} и {y} — друзья",
    "spouses": "{x} и {y} — супруги",
    "x_parent_of_y": "{x} — родитель для {y}",
    "y_parent_of_x": "{x} — ребёнок для {y}",
    "x_client_of_y": "{x} — клиент для {y}",
    "y_client_of_x": "{y} — клиент для {x}",
    "x_vendor_of_y": "{x} — поставщик для {y}",
    "y_vendor_of_x": "{y} — поставщик для {x}",
}


def manual_roles_json() -> str:
    roles = [{"key": k, "text": ROLE_TEXT[k]} for k in MANUAL_ROLES]
    return json.dumps(roles, ensure_ascii=False).replace("</", "<\\/")
