"""Группировка отпечатков в говорящих внутри одного разговора.

Агломеративная кластеризация со средней связью: начинаем с того, что каждая
реплика — свой голос, и сливаем ближайшие пары, пока они похожи сильнее
порога. Без sklearn: лишняя зависимость на 30 МБ ради полусотни строк не нужна.

Средняя связь, а не ближняя: одна случайная пара похожих реплик от разных
людей не должна склеивать две группы целиком. И не дальняя — та наоборот
дробит одного человека, стоит ему один раз сказать что-то неразборчиво.

## Почему на матрице, а не в лоб

Наивная версия пересчитывала похожесть всех пар векторов на каждой итерации.
Замер: 500 реплик — **38 секунд**, и это не выдуманный размер, столько было
на настоящем созвоне (507 реплик удалённой стороны). Тридцать восемь секунд
блокировали бы поток распознавания на закрытии разговора.

Здесь похожести считаются ОДИН раз матричным умножением, а при слиянии
пересчитываются по формуле Лэнса—Уильямса: средняя связь объединения —
взвешенное среднее связей слагаемых, без возврата к векторам. Результат тот
же с точностью до чисел с плавающей точкой (проверено тестом против наивной
реализации), время на тех же 500 репликах — **0.22 секунды** вместо 38.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from vera_listener.speakers.embedder import normalize

#: Порог слияния. Замер на голосах TTS (2026-09-02): свои пары 0.878–0.890,
#: чужие 0.162–0.499. 0.65 стоит с запасом от обеих границ.
#:
#: ЧЕСТНАЯ ОГОВОРКА: TTS-голоса различаются сильнее живых. Два мужских голоса
#: на плохой связи будут ближе, чем Ирина и Дэвид. Порог придётся проверять
#: на настоящих созвонах — здесь он вынесен в параметр именно поэтому, а не
#: закопан в тело функции.
MERGE_THRESHOLD = 0.65

#: Больше этого говорящих в одном разговоре не ищем. Не ограничение модели,
#: а защита от дробления: на шумной записи кластеризация склонна плодить
#: «новых людей» из обрывков, и десяток фантомов хуже, чем несколько слитых.
MAX_SPEAKERS = 8

#: Ниже этой похожести не сливаем ДАЖЕ ради потолка говорящих. Два голоса с
#: отрицательной или near-нулевой похожестью — заведомо разные люди, и
#: склеить их значит соврать, а не упростить. Лучше оставить говорящих больше
#: потолка, чем приписать одному человеку чужие слова. Нашло ревью.
FORCED_MERGE_FLOOR = 0.3


@dataclass(frozen=True)
class Cluster:
    """Группа реплик, признанных одним голосом."""

    members: tuple[int, ...]
    centroid: np.ndarray

    def __len__(self) -> int:
        return len(self.members)


def cluster_embeddings(embeddings: list[np.ndarray], *,
                       threshold: float = MERGE_THRESHOLD,
                       max_speakers: int = MAX_SPEAKERS,
                       forced_floor: float = FORCED_MERGE_FLOOR) -> list[Cluster]:
    """Отпечатки → группы. Порядок групп — по убыванию числа реплик.

    Индексы в `members` — позиции во ВХОДНОМ списке, а не порядковые номера
    говорящих: вызывающий сам решает, как их назвать.
    """
    if not embeddings:
        return []

    vectors = np.stack([normalize(np.asarray(e, dtype=np.float32))
                        for e in embeddings])
    count = len(vectors)
    # Векторы единичные, поэтому их скалярные произведения и есть косинусы.
    sims = (vectors @ vectors.T).astype(np.float32)
    np.fill_diagonal(sims, -np.inf)

    members: list[list[int]] = [[i] for i in range(count)]
    sizes = np.ones(count, dtype=np.float32)
    alive = np.ones(count, dtype=bool)

    while int(alive.sum()) > 1:
        flat = int(np.argmax(np.where(alive[:, None] & alive[None, :], sims, -np.inf)))
        left, right = divmod(flat, count)
        best = float(sims[left, right])
        if not _should_merge(best, int(alive.sum()), threshold, max_speakers,
                             forced_floor):
            break
        _merge(sims, sizes, alive, members, left, right)

    clusters = [
        Cluster(members=tuple(sorted(members[i])),
                centroid=normalize(vectors[members[i]].mean(axis=0)))
        for i in range(count) if alive[i]
    ]
    return sorted(clusters, key=lambda c: (-len(c), c.members[0]))


def _should_merge(best: float, groups: int, threshold: float,
                  max_speakers: int, forced_floor: float) -> bool:
    """Сливать ли ближайшую пару.

    Три случая, и средний — единственный неочевидный:
    1. похожи сильнее порога — сливаем всегда;
    2. слабее порога, но групп больше потолка — сливаем ПРИНУДИТЕЛЬНО, чтобы
       шумная запись не рассыпалась на фантомов, — но только пока пара хоть
       сколько-то похожа (`forced_floor`);
    3. слабее порога и групп уже не больше потолка — стоп.
    """
    if best >= threshold:
        return True
    if groups > max_speakers:
        return best >= forced_floor
    return False


def _merge(sims: np.ndarray, sizes: np.ndarray, alive: np.ndarray,
           members: list[list[int]], left: int, right: int) -> None:
    """Слить группу `right` в `left`, пересчитав связи по Лэнсу—Уильямсу.

    Средняя связь объединения к любой третьей группе — среднее связей
    слагаемых, взвешенное их размерами. Возвращаться к самим векторам не
    нужно, и в этом вся экономия.
    """
    total = sizes[left] + sizes[right]
    blended = (sims[left] * sizes[left] + sims[right] * sizes[right]) / total
    sims[left] = blended
    sims[:, left] = blended
    sims[left, left] = -np.inf

    members[left] = members[left] + members[right]
    sizes[left] = total
    alive[right] = False
    sims[right, :] = -np.inf
    sims[:, right] = -np.inf
