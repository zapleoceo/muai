"""Группировка отпечатков в говорящих.

Векторы здесь синтетические и намеренно простые: цель — проверить логику
слияния, а не качество модели. Качество модели проверено замером на живых
голосах и записано в `embedder.py`.
"""
from __future__ import annotations

import numpy as np

from vera_listener.speakers.cluster import (
    FORCED_MERGE_FLOOR,
    cluster_embeddings,
)
from vera_listener.speakers.embedder import normalize


def _vec(*values: float) -> np.ndarray:
    """Единичный вектор из первых координат, остальное — нули."""
    full = np.zeros(8, dtype=np.float32)
    full[:len(values)] = values
    return normalize(full)


#: Три попарно ортогональных направления — заведомо разные «голоса».
A, B, C = _vec(1, 0, 0), _vec(0, 1, 0), _vec(0, 0, 1)


def _near(base: np.ndarray, noise: float, seed: int) -> np.ndarray:
    """Тот же голос с небольшим отклонением."""
    rng = np.random.default_rng(seed)
    return normalize(base + rng.normal(0, noise, base.shape).astype(np.float32))


class TestBasics:
    def test_empty_input_gives_no_clusters(self):
        assert cluster_embeddings([]) == []

    def test_single_embedding_is_one_cluster(self):
        clusters = cluster_embeddings([A])
        assert len(clusters) == 1
        assert clusters[0].members == (0,)

    def test_members_are_indices_into_the_input(self):
        clusters = cluster_embeddings([A, B])
        assert {m for c in clusters for m in c.members} == {0, 1}


class TestSeparation:
    def test_two_distinct_voices_stay_apart(self):
        clusters = cluster_embeddings([A, A, B, B])
        assert len(clusters) == 2
        assert {frozenset(c.members) for c in clusters} == {
            frozenset({0, 1}), frozenset({2, 3})}

    def test_same_voice_with_noise_merges(self):
        clusters = cluster_embeddings([_near(A, 0.15, s) for s in range(4)])
        assert len(clusters) == 1

    def test_three_voices_give_three_clusters(self):
        clusters = cluster_embeddings([A, A, B, B, C, C])
        assert len(clusters) == 3

    def test_threshold_controls_merging(self):
        """Порог — параметр, а не магия внутри: на нём калибруют под живую
        речь, где голоса ближе, чем синтетические."""
        pair = [A, _near(A, 0.5, 1)]
        assert len(cluster_embeddings(pair, threshold=0.99)) == 2
        assert len(cluster_embeddings(pair, threshold=0.1)) == 1


class TestOrdering:
    def test_largest_cluster_comes_first(self):
        """Порядок — не косметика: по нему раздаются номера «Собеседник N»,
        и самый говорливый должен быть первым."""
        clusters = cluster_embeddings([A, A, A, B])
        assert len(clusters[0]) == 3
        assert len(clusters[1]) == 1

    def test_ties_break_by_first_appearance(self):
        """Одинаковые по размеру — в порядке появления, иначе номера
        плясали бы от запуска к запуску на одних и тех же данных."""
        clusters = cluster_embeddings([A, B])
        assert clusters[0].members[0] < clusters[1].members[0]


class TestCentroid:
    def test_centroid_is_unit_length(self):
        """Иначе скалярное произведение перестаёт быть косинусом, и порог
        узнавания начинает зависеть от числа реплик в группе."""
        for cluster in cluster_embeddings([_near(A, 0.1, s) for s in range(3)]):
            assert abs(float(np.linalg.norm(cluster.centroid)) - 1.0) < 1e-5

    def test_centroid_is_close_to_its_members(self):
        cluster = cluster_embeddings([_near(A, 0.1, s) for s in range(3)])[0]
        assert float(np.dot(cluster.centroid, A)) > 0.9


class TestSpeakerCap:
    def test_cap_forces_merging_of_similar_voices(self):
        """На шумной записи кластеризация плодит фантомов; потолок сливает
        ближайших, потому что десяток выдуманных людей хуже нескольких слитых.

        Но только ПОХОЖИХ — заведомо разные голоса потолок не склеивает,
        см. `TestForcedMergeFloor`."""
        rng = np.random.default_rng(4)
        base = normalize(rng.normal(0, 1, 32).astype(np.float32))
        alike = [normalize(base + rng.normal(0, 0.3, 32).astype(np.float32))
                 for _ in range(10)]
        assert len(cluster_embeddings(alike, max_speakers=3)) <= 3

    def test_below_the_cap_nothing_is_forced(self):
        assert len(cluster_embeddings([A, B], max_speakers=5)) == 2


def _naive(vectors, threshold, max_speakers, floor):
    """Прямолинейная агломерация: пересчитывает связи от самих векторов.

    Эталон для проверки матричной версии. Медленная (38с на 500 репликах —
    ровно поэтому её и заменили), но очевидно правильная.
    """
    groups = [[i] for i in range(len(vectors))]

    def link(left, right):
        total = sum(float(np.dot(vectors[a], vectors[b])) for a in left for b in right)
        return total / (len(left) * len(right))

    while len(groups) > 1:
        best, pair = -np.inf, None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                score = link(groups[i], groups[j])
                if score > best:
                    best, pair = score, (i, j)
        merge = best >= threshold or (len(groups) > max_speakers and best >= floor)
        if not merge:
            break
        i, j = pair
        groups[i] = groups[i] + groups[j]
        groups.pop(j)
    return sorted(tuple(sorted(g)) for g in groups)


class TestMatchesNaiveImplementation:
    """Матричная версия обязана давать ТО ЖЕ, что прямолинейная.

    Оптимизация ради скорости (38с → 0.22с на 500 репликах) не имеет права
    менять результат: связи пересчитываются по формуле Лэнса—Уильямса, а не
    заново от векторов, и эквивалентность этих двух путей надо доказать, а
    не предположить.
    """

    def _random(self, count, speakers, sigma, seed):
        rng = np.random.default_rng(seed)
        bases = [normalize(rng.normal(0, 1, 32).astype(np.float32))
                 for _ in range(speakers)]
        return [normalize(bases[i % speakers]
                          + rng.normal(0, sigma, 32).astype(np.float32))
                for i in range(count)]

    def test_same_grouping_on_clean_data(self):
        vectors = self._random(24, speakers=3, sigma=0.1, seed=1)
        mine = sorted(c.members for c in cluster_embeddings(vectors))
        assert mine == _naive(vectors, 0.65, 8, FORCED_MERGE_FLOOR)

    def test_same_grouping_on_noisy_data(self):
        vectors = self._random(20, speakers=4, sigma=0.35, seed=2)
        mine = sorted(c.members for c in cluster_embeddings(vectors))
        assert mine == _naive(vectors, 0.65, 8, FORCED_MERGE_FLOOR)

    def test_same_grouping_when_the_cap_forces_merges(self):
        vectors = self._random(18, speakers=9, sigma=0.05, seed=3)
        mine = sorted(c.members for c in cluster_embeddings(vectors, max_speakers=3))
        assert mine == _naive(vectors, 0.65, 3, FORCED_MERGE_FLOOR)


class TestForcedMergeFloor:
    """Потолок говорящих не должен склеивать заведомо разных людей."""

    def test_dissimilar_voices_survive_the_cap(self):
        """Восемь взаимно непохожих голосов при потолке 3: слить их значило бы
        приписать одному человеку чужие слова. Лучше говорящих больше
        потолка, чем ложь в авторстве."""
        singles = [normalize(np.eye(8, dtype=np.float32)[i]) for i in range(8)]
        clusters = cluster_embeddings(singles, max_speakers=3)
        assert len(clusters) == 8

    def test_cap_is_a_target_not_a_guarantee(self):
        """Пол сильнее потолка, и это осознанно.

        Замер на этих данных: без потолка 8 групп, с потолком 2 — четыре, а
        не две. Слияние останавливается, когда очередная пара расходится
        ниже пола: по мере укрупнения групп их средняя связь падает. Значит
        потолок — цель, а не обещание, и это лучше обратного: досчитать до
        ровно двух можно было бы только склеив заведомо разных людей."""
        rng = np.random.default_rng(7)
        base = normalize(rng.normal(0, 1, 32).astype(np.float32))
        alike = [normalize(base + rng.normal(0, 0.3, 32).astype(np.float32))
                 for _ in range(10)]
        free = len(cluster_embeddings(alike))
        capped = len(cluster_embeddings(alike, max_speakers=2))
        assert capped < free, "потолок обязан сокращать число голосов"
        assert capped > 2, "но не ценой слияния ниже пола"

    def test_without_the_floor_the_cap_is_reached_exactly(self):
        """Контроль: ограничивает именно пол, а не изъян в потолке."""
        rng = np.random.default_rng(7)
        base = normalize(rng.normal(0, 1, 32).astype(np.float32))
        alike = [normalize(base + rng.normal(0, 0.3, 32).astype(np.float32))
                 for _ in range(10)]
        assert len(cluster_embeddings(alike, max_speakers=2, forced_floor=-1.0)) == 2

    def test_floor_is_configurable(self):
        singles = [normalize(np.eye(8, dtype=np.float32)[i]) for i in range(8)]
        loose = cluster_embeddings(singles, max_speakers=2, forced_floor=-1.0)
        assert len(loose) == 2


class TestSpeed:
    """Скорость — часть контракта: кластеризация идёт на закрытии разговора,
    в потоке распознавания. Наивная версия давала 38с на 500 репликах, и
    столько их было на настоящем созвоне."""

    def test_realistic_session_is_fast(self):
        import time
        rng = np.random.default_rng(11)
        bases = [normalize(rng.normal(0, 1, 256).astype(np.float32)) for _ in range(4)]
        vectors = [normalize(bases[i % 4]
                             + rng.normal(0, 0.034, 256).astype(np.float32))
                   for i in range(500)]
        started = time.monotonic()
        clusters = cluster_embeddings(vectors)
        elapsed = time.monotonic() - started
        assert len(clusters) == 4
        assert elapsed < 5.0, f"кластеризация 500 реплик заняла {elapsed:.1f}с"
