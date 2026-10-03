"""Типы вывода ролей пары из истории переписки — данные без логики."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Канонический набор ролей: тот же, что у `rel_canon` (`reports_to` и `child_of` хранятся
#: как `boss_of` / `parent_of` с переставленными концами, поэтому их здесь нет).
PREDICATES = ("boss_of", "parent_of", "coworker_of", "co_founder_of", "friend_of",
              "spouse_of", "client_of", "vendor_of")
A_TO_B, B_TO_A, BOTH = "a_to_b", "b_to_a", "both"
KIND_DM, KIND_CHAT, KIND_MAIL, KIND_MENTION = "dm", "chat", "mail", "mention"


@dataclass(frozen=True)
class PackMessage:
    """Одно сообщение пакета улик. `author` — метка «A», «B» или «X» (третье лицо)."""
    id: str
    at: str
    kind: str
    author: str
    text: str
    channel: str = ""
    about: str = ""           # у упоминаний третьими лицами: о ком речь (A или B)
    score: float = 0.0


@dataclass(frozen=True)
class PairSide:
    """Метка и имя одного конца пары в пакете (A — меньший id, B — больший)."""
    label: str
    entity_id: int
    name: str
    is_owner: bool = False
    addresses: tuple[str, ...] = ()
    nicknames: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    """Готовый пакет: концы пары, структурные сигналы, отобранные сообщения, хэш."""
    a: PairSide
    b: PairSide
    signals: dict[str, Any]
    messages: tuple[PackMessage, ...]
    digest: str
    corpus: str = ""          # весь текст пакета для проверки цитат


@dataclass(frozen=True)
class RoleFinding:
    predicate: str
    direction: str             # a_to_b | b_to_a | both — относительно упорядоченной пары
    confidence: float
    rationale: str
    quotes: tuple[str, ...]


@dataclass(frozen=True)
class PairInference:
    entity_a: int
    entity_b: int
    roles: tuple[RoleFinding, ...] = ()
    summary: str = ""
    model: str = ""
    digest: str = ""
    cost_usd: float = 0.0
    skipped: str = ""          # причина, по которой модель не звали
    failed: bool = False       # брокер не ответил (в том числе открытый брейкер): цикл стоит
    bad_format: bool = False   # ответ не по схеме: пара уходит в паузу, цикл идёт дальше
