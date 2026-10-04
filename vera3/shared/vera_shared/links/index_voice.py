"""Связи созвона: участники по голосу и карте голосов, названные в выжимке, ASR-угадывания.

Поиск искажённых имён — CPU (`asr.asr_matches_async` в отдельном потоке), поэтому сборка
созвона разделена: `voice_guesses` (async, тяжёлое) и `voice_links_for` (чистая).
"""
from __future__ import annotations

from vera_shared.links.asr import AsrMatch, asr_matches_async
from vera_shared.links.builders import is_anonymous, voice_links
from vera_shared.links.context_data import EventFacts, EventView
from vera_shared.links.index_resources import Resources
from vera_shared.links.index_store import MAX_VOICE_CHARS
from vera_shared.links.model import MANUAL, MENTIONED, NAME_MATCH, VOICEPRINT, Link
from vera_shared.links.scope import GLOBAL

FULL_NAME_CONFIDENCE, SHORT_NAME_CONFIDENCE = 0.9, 0.6


def voice_body(view: EventView) -> str:
    return (view.transcript or view.text)[:MAX_VOICE_CHARS]


async def voice_guesses(view: EventView, res: Resources) -> list[AsrMatch]:
    return await asr_matches_async(voice_body(view), res.asr_candidates)


def _voiceprints(view: EventView) -> dict[str, str]:
    """ярлык говорящего → id отпечатка, если слушатель его прислал (будущее поле реплики)."""
    return {str(u["speaker"]): str(u["voiceprint"])
            for u in (view.extra or {}).get("utterances") or []
            if u.get("speaker") and u.get("voiceprint")}


def voice_links_for(view: EventView, owner: int | None, res: Resources,
                    guessed: list[AsrMatch]) -> list[Link]:
    prints = _voiceprints(view)

    def speaker(label: str) -> tuple[int, str, float] | None:
        if (eid := res.speaker_map.get(("event", f"{view.id}:{label}"))) is not None:
            return eid, MANUAL, 1.0
        if label in prints and (eid := res.speaker_map.get(("voiceprint", prints[label]))):
            return eid, VOICEPRINT, 1.0
        found = None if is_anonymous(label) else res.names.resolve(label, res.owner_circle)
        if found is None:
            return None
        confidence = FULL_NAME_CONFIDENCE if found[1] == "full" else SHORT_NAME_CONFIDENCE
        return found[0], VOICEPRINT if found[1] == "full" else NAME_MATCH, confidence

    def counterpart(name: str) -> int | None:
        """Участник из выжимки: имя, одиночное имя / фамилия в круге владельца, затем прозвище
        области global («Дмитрий Александрович» — фраза, которую владелец привязал к человеку)."""
        found = res.names.resolve(name, res.owner_circle)
        if found:
            return found[0]
        nick = res.matcher.find(name, EventFacts().ctx, owner, scopes=frozenset({GLOBAL}))
        return nick[0].entity_id if len(nick) == 1 else None

    exact = res.matcher.find(voice_body(view), EventFacts().ctx, owner, scopes=frozenset({GLOBAL}))
    known = {m.entity_id for m in exact}
    links = voice_links(view, owner, speaker, counterpart,
                        [m for m in guessed if m.entity_id not in known])
    return links + [Link(view.id, m.entity_id, MENTIONED, NAME_MATCH, m.confidence, m.token[:120])
                    for m in exact]
