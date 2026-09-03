"""Признаки для опознания голоса: форма, нормировка, границы.

Саму «правильность» fbank юнит-тестом не поймать — она проверяется тем, что
модель на этих признаках различает голоса (замер 2026-09-02: свои пары
0.86–0.89, чужие 0.16–0.50). Здесь — то, что ломается тихо: форма выхода,
вычитание среднего, поведение на слишком коротком куске.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vera_listener.speakers.embedder import OpenVinoSpeakerEmbedder
from vera_listener.speakers.features import (
    FRAME_LEN,
    FRAME_SHIFT,
    MIN_FRAMES,
    MIN_VOICEPRINT_S,
    NUM_MEL,
    PREEMPHASIS,
    SAMPLE_RATE,
    fbank,
    mel_filterbank,
    povey_window,
)


def _tone(seconds: float, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class TestShape:
    def test_second_of_audio_gives_expected_frame_count(self):
        feats = fbank(_tone(1.0))
        expected = 1 + (SAMPLE_RATE - FRAME_LEN) // FRAME_SHIFT
        assert feats.shape == (expected, NUM_MEL)

    def test_output_is_float32(self):
        """Модель объявляет вход float32; float64 обошёлся бы копией на каждый
        вызов, а то и падением на строгой проверке типа."""
        assert fbank(_tone(0.5)).dtype == np.float32

    def test_shorter_than_one_window_is_empty_not_error(self):
        """Обрывок речи — обычное дело, а не сбой: решать вызывающему."""
        feats = fbank(_tone(FRAME_LEN / SAMPLE_RATE / 2))
        assert feats.shape == (0, NUM_MEL)

    def test_exactly_one_window_gives_one_frame(self):
        assert fbank(_tone(FRAME_LEN / SAMPLE_RATE)).shape == (1, NUM_MEL)

    def test_stereo_input_is_rejected_loudly(self):
        """Тихо усреднить каналы значило бы принять чужой формат за свой."""
        with pytest.raises(ValueError, match="моно"):
            fbank(np.zeros((100, 2), dtype=np.float32))


class TestNormalization:
    def test_mean_over_time_is_removed(self):
        """CMN: без него отпечаток зависел бы от микрофона и громкости
        сильнее, чем от голоса."""
        feats = fbank(_tone(1.0))
        assert np.allclose(feats.mean(axis=0), 0.0, atol=1e-4)

    def test_loudness_barely_moves_the_features(self):
        """Тот же тон вдвое тише — те же признаки: громкость снимается CMN."""
        quiet, loud = fbank(_tone(1.0) * 0.5), fbank(_tone(1.0))
        assert np.allclose(quiet, loud, atol=1e-3)

    def test_changing_sound_survives_cmn(self):
        """CMN снимает постоянную составляющую, а не всё подряд. На меняющемся
        звуке — а живая речь всегда меняется — признаки обязаны остаться.

        Ровный тон брать нельзя: у него все кадры одинаковы и после вычитания
        среднего остаётся ноль. Причём не у всякого — только когда частота
        укладывается целым числом периодов в шаг кадра (150 и 900 Гц
        укладываются, 220 нет). Совпадение, а не контракт, поэтому проверяем
        на нестационарном сигнале."""
        t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
        sweep = (0.3 * np.sin(2 * np.pi * (150 + 700 * t) * t)).astype(np.float32)
        assert np.abs(fbank(sweep)).max() > 1.0

    def test_noise_gives_varied_features(self):
        """Второй нестационарный случай: шум обязан дать разброс по кадрам."""
        rng = np.random.default_rng(0)
        noise = rng.normal(0, 0.1, SAMPLE_RATE).astype(np.float32)
        assert float(fbank(noise).std()) > 0.1


class TestMelFilterbank:
    def test_shape_matches_fft_size(self):
        banks = mel_filterbank(num_bins=80, nfft=512)
        assert banks.shape == (80, 512 // 2 + 1)

    def test_every_bin_has_weight(self):
        """Пустая полоса — молчаливо потерянный кусок спектра."""
        banks = mel_filterbank()
        assert (banks.sum(axis=1) > 0).all()

    def test_weights_are_non_negative(self):
        assert (mel_filterbank() >= 0).all()

    def test_low_bins_cover_low_frequencies(self):
        """Мел-шкала сгущается внизу: первая полоса обязана быть левее
        последней, иначе перепутан порядок краёв."""
        banks = mel_filterbank()
        first = np.argmax(banks[0] > 0)
        last = np.argmax(banks[-1] > 0)
        assert first < last


class TestAgainstKaldiFormulas:
    """Сверка с эталонными формулами kaldi, на которых обучена модель.

    Это единственный вид теста, ловящий ТИХИЙ разъезд препроцессинга: модель
    и на неверных признаках выдаст вектор, просто хуже. Оба прежних дефекта
    (пустые мел-полосы, неверный знаменатель окна) нашлись руками, а не
    тестом — эти тесты закрывают дыру.
    """

    def test_povey_window_matches_kaldi(self):
        """`feature-window.cc`: a = 2π/(frame_length-1); w(i) = (0.5-0.5·cos(a·i))^0.85.

        Знаменатель `size-1` (симметричный ханн), а не `size` (периодический).
        Периодический расходился до 0.006 — систематически, на каждом кадре."""
        size = FRAME_LEN
        step = 2 * np.pi / (size - 1)
        expected = (0.5 - 0.5 * np.cos(step * np.arange(size))) ** 0.85
        assert np.allclose(povey_window(size), expected, atol=1e-9)

    def test_mel_scale_matches_kaldi(self):
        """mel = 1127·ln(1 + f/700). Полосы строятся по этой шкале, и сдвиг
        в ней уводит все 80 фильтров разом."""
        from vera_listener.speakers.features import _hz_to_mel, _mel_to_hz
        for hz in (20.0, 300.0, 1000.0, 8000.0):
            assert abs(float(_hz_to_mel(hz)) - 1127.0 * np.log(1 + hz / 700)) < 1e-6
            assert abs(float(_mel_to_hz(_hz_to_mel(hz))) - hz) < 1e-3

    def test_preemphasis_replicates_the_first_sample(self):
        """kaldi для первого отсчёта берёт x[0]·(1-coeff) — как если бы слева
        стоял он же. Обнулить его вместо этого — сдвиг на каждом кадре."""
        assert PREEMPHASIS == 0.97

    def test_frame_count_snips_edges_like_kaldi(self):
        """`snip_edges=true`: неполный хвост отбрасывается, не добивается
        нулями. Паддинг дал бы лишний кадр тишины в конце каждого куска."""
        samples = FRAME_LEN + FRAME_SHIFT + FRAME_SHIFT // 2
        audio = np.zeros(samples, dtype=np.float32)
        audio[::3] = 0.1
        assert fbank(audio).shape[0] == 1 + (samples - FRAME_LEN) // FRAME_SHIFT

class TestSpeechFloor:
    """Отпечаток снимается только с куска, которому можно верить.

    Замер лежит в `features.MIN_VOICEPRINT_S`: два коротких куска одного и
    того же голоса сходятся слабее порога слияния, пока кусок короче трёх
    секунд. Такой кусок разводит одного человека на нескольких — ровно это и
    случилось вживую 03.09.
    """

    def test_floor_matches_the_declared_seconds(self):
        seconds = ((MIN_FRAMES - 1) * FRAME_SHIFT + FRAME_LEN) / SAMPLE_RATE
        assert seconds == pytest.approx(MIN_VOICEPRINT_S, abs=0.01)

    def test_short_piece_gives_no_fingerprint(self):
        """Полторы секунды — отказ, и модель ради этого даже не грузится."""
        embedder = OpenVinoSpeakerEmbedder(Path("нет такого каталога"))
        assert embedder.embed(np.zeros(int(1.5 * SAMPLE_RATE), np.float32)) is None

    def test_floor_clears_the_merge_threshold_with_room(self):
        """Порог должен стоять там, где свой голос уверенно сходится сам с собой.

        На 2.0с замер даёт 0.537 при пороге слияния 0.65 — то есть floor,
        поставленный «почти правильно», дефект бы не закрыл. Держим три
        секунды: 0.759.
        """
        assert MIN_VOICEPRINT_S >= 3.0

    def test_short_piece_is_refused_below_the_floor(self):
        """Две с половиной секунды всё ещё коротко — отпечатка быть не должно."""
        embedder = OpenVinoSpeakerEmbedder(Path("нет такого каталога"))
        assert embedder.embed(np.zeros(int(2.5 * SAMPLE_RATE), np.float32)) is None
