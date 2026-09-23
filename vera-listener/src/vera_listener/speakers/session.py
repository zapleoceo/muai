"""Кто говорил в этом разговоре: сбор отпечатков и раздача имён.

Порядок работы: по ходу разговора копим отпечатки реплик удалённой стороны,
на закрытии — кластеризуем и называем. Кластеризация именно в конце, а не на
лету: чем больше реплик, тем устойчивее группы, а имя реплике нужно только
перед отправкой.

Откуда берутся имена, по убыванию надёжности:

1. **Знакомый отпечаток** — этот голос уже звучал в разговоре, где имя было
   известно из заголовка окна.
2. **Заголовок окна один на один** — Slack и Telegram называют собеседника.
   Здесь важно, ПОДТВЕРДИЛО ли приложение личку:

   * Подтвердило (Slack) — имя ставится всем репликам, сколько бы кластеров
     ни насчиталось. До 16.09 было наоборот («голосов много — значит обещание
     нарушено»), и правило снято по данным: на живой речи кластеризация дробит
     ОДНОГО человека, поэтому «кластеров много» перестало быть признаком того,
     что людей много. Исключение (23.09) — узнан голос с именем НЕ из
     заголовка: это доказательство, что на линии больше одного человека
     (хадл в личке). Тогда узнанным — их имена; неузнанным — имя из
     заголовка, если сам хозяин лички не узнан (его голос и дробится на
     осколки), иначе «Собеседник N». Отпечаток в таком разговоре не
     запоминается. Случай 21.09: личка Виктора, в хадле ещё Вадим — до
     правки все 26 реплик ушли Вадиму.
   * Не подтвердило (Telegram) — имя только когда голос действительно один.
     Форма заголовка звонка личку не доказывает: среди реальных заголовков
     есть «General @ …», то есть групповой чат.
3. **Порядковый номер** — «Собеседник 1». Не поражение, а честный ответ:
   реплики разделены по голосам, просто имя взять неоткуда.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np

from vera_listener.counterpart import Counterpart
from vera_listener.speakers import journal
from vera_listener.speakers.cluster import (
    MAX_SPEAKERS,
    MERGE_THRESHOLD,
    Cluster,
    cluster_embeddings,
)
from vera_listener.speakers.embedder import SpeakerEmbedder
from vera_listener.speakers.registry import VoiceprintRegistry
from vera_listener.speakers.speech import keep_speech

log = logging.getLogger("listener.speakers")

#: Как зовём голос, для которого имени не нашлось.
UNKNOWN_PREFIX = "Собеседник"


class SpeakerSession:
    """Отпечатки одного разговора. Живёт от начала сессии до отправки."""

    def __init__(self, embedder: SpeakerEmbedder, registry: VoiceprintRegistry, *,
                 threshold: float = MERGE_THRESHOLD,
                 max_speakers: int = MAX_SPEAKERS,
                 trim: Callable[[np.ndarray], np.ndarray] = keep_speech,
                 journal_dir: Path | None = None):
        self._embedder = embedder
        self._registry = registry
        self._trim = trim
        self._journal_dir = journal_dir
        self._threshold = threshold
        self._max_speakers = max_speakers
        self._keys: list[float] = []
        self._embeddings: list[np.ndarray] = []
        #: Смещения реплик, у которых отпечаток БЫЛ, но голосом не подтвердился
        #: (см. `cluster._voices_only`). Это НЕ то же самое, что реплика без
        #: отпечатка: там имя можно достроить по единственному голосу, здесь
        #: нельзя — вектор как раз и не сошёлся ни с кем. Заполняется в
        #: `resolve`, читается при раздаче имён.
        self.unconfirmed: set[float] = set()

    def observe(self, at: float, audio: np.ndarray) -> None:
        """Запомнить отпечаток реплики удалённой стороны.

        Сбой опознания не должен ронять разговор: текст уже распознан и
        ценнее любой разметки говорящих, поэтому ошибка гасится здесь.
        """
        try:
            # Тишину из куска убираем ДО модели: границы реплики whisper даёт
            # с запасом, а отпечаток усредняется по всему куску — см. замер в
            # `speech.py`. Порог длины тогда мерит речь, а не паузы вокруг неё.
            #
            # Обрезка внедряется, а не зашита, по той же причине, что и сам
            # опознаватель: она зовёт silero, и тесту наименования незачем
            # тащить за собой распознавание речи, чтобы проверить раздачу имён.
            vector = self._embedder.embed(self._trim(audio))
        except Exception as e:                          # noqa: BLE001
            log.warning("отпечаток голоса не снялся (%s) — реплика без имени",
                        type(e).__name__)
            return
        if vector is None:
            return
        self._keys.append(round(float(at), 2))
        self._embeddings.append(vector)

    def resolve(self, counterpart: Counterpart | None = None,
                app: str | None = None) -> dict[float, str]:
        """Кластеризовать и назвать. → {смещение реплики: имя говорящего}."""
        if not self._embeddings:
            return {}

        clusters = cluster_embeddings(self._embeddings, threshold=self._threshold,
                                      max_speakers=self._max_speakers)

        # Приложение подтвердило личку — значит на дорожке собеседника ОДИН
        # человек, сколько бы кластеров ни насчиталось. Спорить с этим
        # кластеризации нечем: на живой речи она дробит одного на нескольких
        # (16.09, разговор один на один: восемь «собеседников», сорок реплик
        # в главном кластере и семь одиночек — и все семь оказались короткими
        # фразами того же человека).
        if counterpart is not None and counterpart.is_direct:
            return self._one_person(clusters, counterpart, app)

        alone = len(clusters) == 1
        names: list[str] = []
        taken: set[str] = set()

        unnamed = 0
        for cluster in clusters:
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
            # Нумеруем безымянных подряд, а не по месту в списке кластеров:
            # «Собеседник 1, Собеседник 3» рядом с названным по имени читается
            # как потерянная реплика, хотя не потеряно ничего.
            unnamed += 1
            names.append(f"{UNKNOWN_PREFIX} {unnamed}")

        return self._finish(clusters, names, counterpart, app)

    def _one_person(self, clusters: list[Cluster], counterpart: Counterpart,
                    app: str | None) -> dict[float, str]:
        """Все реплики — хозяину лички, если отпечатки не услышали другого."""
        # Отпечаток надёжнее заголовка и здесь: заголовок говорит, какое окно
        # впереди, а отпечаток — чей это голос. Порог узнавания 0.72 высокий,
        # поэтому совпадение с ним — сильное свидетельство.
        recognised = [(len(c.members), name) for c in clusters
                      if (name := self._registry.match(c.centroid))]

        # Узнан голос с именем НЕ из заголовка — значит, на линии больше одного
        # человека: хадл в личке, куда позвали третьего (21.09: личка Виктора,
        # отпечаток узнал Вадима, и его имя легло на все 26 реплик). Сводить
        # к одному имени здесь нельзя ни к чьему.
        if any(name != counterpart.name for _, name in recognised):
            return self._huddle(clusters, counterpart, app)

        name = counterpart.name

        # Назвать и ЗАПОМНИТЬ — решения разной цены. Имя живёт один разговор и
        # видно глазами; отпечаток уходит в постоянное хранилище, и ошибка в
        # нём зовёт человека чужим именем годами. Поэтому запоминаем, только
        # когда кластеризация САМА согласна, что голос один: иначе в центроид
        # мог бы попасть второй человек, которого приложение не показало.
        if len(clusters) == 1 and not recognised:
            self._registry.remember(counterpart.name, clusters[0].centroid)
            self._registry.save()
        elif len(clusters) > 1:
            log.info("личка подтверждена приложением: %d кластеров сведены к "
                     "одному голосу (%s), отпечаток не запоминаем",
                     len(clusters), name)
        return self._finish(clusters, [name] * len(clusters), counterpart, app)

    def _huddle(self, clusters: list[Cluster], counterpart: Counterpart,
                app: str | None) -> dict[float, str]:
        """Хадл в подтверждённой личке: узнанным — их имена, прочим — см. ниже."""
        known = [self._registry.match(c.centroid) for c in clusters]
        # Хозяин лички на линии почти наверняка — это ЕГО окно. Если отпечатки
        # объяснили всех остальных, а его голос не узнан, неузнанные кластеры —
        # это он: живая речь дробит одного человека на несколько кластеров
        # (16.09), так что «неузнанных несколько» не значит «людей несколько».
        # Если же и хозяин узнан сам, неузнанный осколок может быть чьим угодно
        # из узнанных — тогда честнее номер, чем чужие слова под его именем.
        owner_heard = counterpart.name in known
        names: list[str] = []
        unnamed = 0
        for name in known:
            if name:
                names.append(name)
            elif not owner_heard:
                names.append(counterpart.name)
            else:
                unnamed += 1
                names.append(f"{UNKNOWN_PREFIX} {unnamed}")
        # Отпечаток НЕ запоминаем: в разговоре несколько человек, и центроид
        # неузнанного кластера мог вобрать чужой голос.
        log.info("личка подтверждена приложением, но узнан другой голос (%s) — "
                 "хадл: %d кластеров, отпечаток не запоминаем",
                 ", ".join(sorted({n for n in known if n})), len(clusters))
        return self._finish(clusters, names, counterpart, app)

    def _finish(self, clusters: list[Cluster], names: list[str],
                counterpart: Counterpart | None,
                app: str | None) -> dict[float, str]:
        mapping: dict[float, str] = {}
        for cluster, name in zip(clusters, names, strict=True):
            for member in cluster.members:
                mapping[self._keys[member]] = name
        self.unconfirmed = {k for k in self._keys if k not in mapping}

        if self._journal_dir is not None:
            journal.write(self._journal_dir, self._embeddings, self._keys, names,
                          [list(c.members) for c in clusters],
                          counterpart=counterpart.name if counterpart else None,
                          app=app)

        log.info("голосов в разговоре: %d (%s)", len(set(names)),
                 ", ".join(dict.fromkeys(names)))
        return mapping

    def __len__(self) -> int:
        """Сколько отпечатков снято. ОСТОРОЖНО: делает пустую сессию ложной по
        истинности, поэтому проверять её существование только `is not None`."""
        return len(self._embeddings)
