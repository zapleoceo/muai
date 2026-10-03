"""Связи события — чистые функции: автор, получатель, участники созвона, упомянутые."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

from vera_shared.links.asr import AsrMatch
from vera_shared.links.context import EventFacts, EventView
from vera_shared.links.matcher import KIND_NICKNAME, KIND_USERNAME, Mention
from vera_shared.links.model import (
    ALIAS,
    AUTHOR,
    MENTIONED,
    NAME_MATCH,
    NICKNAME,
    PARTICIPANT,
    RECIPIENT,
    Link,
)

#: Имена, под которыми слушатель называет неопознанный голос: связи не дают.
ANONYMOUS_PREFIXES = ("собеседник", "speaker", "unknown", "голос")
#: Ярлык говорящего → (сущность, источник связи, уверенность) или None.
SpeakerResolver = Callable[[str], tuple[int, str, float] | None]


def base_links(view: EventView, facts: EventFacts) -> list[Link]:
    """Автор и получатели по алиасам (личка, письмо)."""
    out = []
    if facts.author is not None:
        out.append(Link(view.id, facts.author, AUTHOR, ALIAS))
    out += [Link(view.id, e, RECIPIENT, ALIAS) for e in facts.recipients]
    return out


def mention_links(view: EventView, mentions: Iterable[Mention],
                  span: dict[str, Any] | None = None) -> list[Link]:
    out = []
    for m in mentions:
        source = NICKNAME if m.kind == KIND_NICKNAME else ALIAS if m.kind == KIND_USERNAME else NAME_MATCH
        out.append(Link(view.id, m.entity_id, MENTIONED, source, m.confidence, m.token[:120],
                        span, m.scope_ok))
    return out


def is_anonymous(label: str) -> bool:
    return label.strip().casefold().startswith(ANONYMOUS_PREFIXES)


def speakers_of(view: EventView) -> Counter[str]:
    """Ярлыки говорящих созвона и число их реплик (метаданные `voices` и сами реплики)."""
    counts: Counter[str] = Counter()
    for u in (view.extra or {}).get("utterances") or []:
        if (speaker := u.get("speaker")) and not u.get("echo"):
            counts[str(speaker)] += 1
    for voice in view.metadata.get("voices") or []:
        counts.setdefault(str(voice), 0)
    return counts


def voice_links(view: EventView, owner: int | None, resolve_speaker: SpeakerResolver,
                resolve_name: Callable[[str], int | None],
                asr: Iterable[AsrMatch] = ()) -> list[Link]:
    """Участники созвона: владелец (записал), опознанные голоса, названные в выжимке
    участники и угаданные по искажённым именам. Неопознанные ярлыки связей не дают."""
    out = [Link(view.id, owner, AUTHOR, ALIAS)] if owner is not None else []
    for label, n in speakers_of(view).items():
        if (hit := resolve_speaker(label)) is None:
            continue
        entity, source, confidence = hit
        if entity != owner:
            out.append(Link(view.id, entity, PARTICIPANT, source, confidence, label[:120],
                            {"speaker": label, "utterances": n}))
    for name in view.metadata.get("counterparts") or []:
        entity = resolve_name(str(name))
        if entity is not None and entity != owner:
            out.append(Link(view.id, entity, PARTICIPANT, NAME_MATCH, 0.7, str(name)[:120]))
    out += [Link(view.id, m.entity_id, MENTIONED, NAME_MATCH, m.confidence, m.heard[:120],
                  {"asr": True, "ratio": m.ratio}) for m in asr]
    return out

