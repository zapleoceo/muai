"""Кто говорил в этом разговоре: сбор отпечатков и раздача имён.

Порядок работы: по ходу разговора копим отпечатки реплик удалённой стороны,
на закрытии — кластеризуем и называем. Кластеризация именно в конце, а не на
лету: чем больше реплик, тем устойчивее группы, а имя реплике нужно только
перед отправкой.

Откуда берутся имена, по убыванию надёжности:

1. **Знакомый отпечаток** — этот голос уже звучал в разговоре, где имя было
   известно из заголовка окна.
2. **Заголовок окна один на один** — Slack и Telegram называют собеседника.
   Берём, только если голос в разговоре ровно один: заголовок обещает
   один-на-один, но если голосов несколько, обещание нарушено, и приписывать
   имя наугад нельзя.
3. **Порядковый номер** — «Собеседник 1». Не поражение, а честный ответ:
   реплики разделены по голосам, просто имя взять неоткуда.
"""
from __future__ import annotations

import logging

import numpy as np

from vera_listener.counterpart import Counterpart
from vera_listener.speakers.cluster import (
    MAX_SPEAKERS,
    MERGE_THRESHOLD,
    cluster_embeddings,
)
from vera_listener.speakers.embedder import SpeakerEmbedder
from vera_listener.speakers.registry import VoiceprintRegistry

log = logging.getLogger("listener.speakers")

#: Как зовём голос, для которого имени не нашлось.
UNKNOWN_PREFIX = "Собеседник"


class SpeakerSession:
    """Отпечатки одного разговора. Живёт от начала сессии до отправки."""

    def __init__(self, embedder: SpeakerEmbedder, registry: VoiceprintRegistry, *,
                 threshold: float = MERGE_THRESHOLD,
                 max_speakers: int = MAX_SPEAKERS):
        self._embedder = embedder
        self._registry = registry
        self._threshold = threshold
        self._max_speakers = max_speakers
        self._keys: list[float] = []
        self._embeddings: list[np.ndarray] = []

    def observe(self, at: float, audio: np.ndarray) -> None:
        """Запомнить отпечаток реплики удалённой стороны.

        Сбой опознания не должен ронять разговор: текст уже распознан и
        ценнее любой разметки говорящих, поэтому ошибка гасится здесь.
        """
        try:
            vector = self._embedder.embed(audio)
        except Exception as e:                          # noqa: BLE001
            log.warning("отпечаток голоса не снялся (%s) — реплика без имени",
                        type(e).__name__)
            return
        if vector is None:
            return
        self._keys.append(round(float(at), 2))
        self._embeddings.append(vector)

    def resolve(self, counterpart: Counterpart | None = None) -> dict[float, str]:
        """Кластеризовать и назвать. → {смещение реплики: имя говорящего}."""
        if not self._embeddings:
            return {}

        clusters = cluster_embeddings(self._embeddings, threshold=self._threshold,
                                      max_speakers=self._max_speakers)
        alone = len(clusters) == 1
        names: list[str] = []
        taken: set[str] = set()

        for index, cluster in enumerate(clusters, start=1):
            known = self._registry.match(cluster.centroid)
            if known and known not in taken:
                names.append(known)
                taken.add(known)
                continue
            # Заголовок окна обещает один-на-один — верим только если голос
            # действительно один. Иначе имя ушло бы случайному кластеру.
            if alone and counterpart and counterpart.name not in taken:
                names.append(counterpart.name)
                taken.add(counterpart.name)
                # Запоминаем НАВСЕГДА только с подтверждением приложения. У
                # Telegram в заголовке имя чата, и группа из двух слов
                # выглядит как человек — такой голос запоминать нельзя.
                if counterpart.is_direct:
                    self._registry.remember(counterpart.name, cluster.centroid)
                    self._registry.save()
                continue
            names.append(f"{UNKNOWN_PREFIX} {index}")

        mapping: dict[float, str] = {}
        for cluster, name in zip(clusters, names, strict=True):
            for member in cluster.members:
                mapping[self._keys[member]] = name

        log.info("голосов в разговоре: %d (%s)", len(clusters), ", ".join(names))
        return mapping

    def __len__(self) -> int:
        """Сколько отпечатков снято. ОСТОРОЖНО: делает пустую сессию ложной по
        истинности, поэтому проверять её существование только `is not None`."""
        return len(self._embeddings)
