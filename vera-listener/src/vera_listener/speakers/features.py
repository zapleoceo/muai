"""Лог-мел признаки (fbank) для модели опознания голоса — на чистом numpy.

Почему не `torchaudio.compliance.kaldi.fbank`, как в оригинальном wespeaker:
torch уже стоит ради ничего (слушатель считает whisper на OpenVINO), а
torchaudio тянет ещё сотни мегабайт ради одной функции. Здесь она в сорок
строк, а корректность проверяется не верой, а замером: на трёх голосах TTS
свои пары дают близость 0.86–0.89, чужие 0.16–0.50. Разъедься preprocessing
с обучением — свои пары не были бы такими близкими.

Параметры повторяют kaldi-умолчания, на которых обучен wespeaker: окно 25 мс,
шаг 10 мс, 80 мел-полос, преэмфазис 0.97, окно Povey, отсечка снизу 20 Гц.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16_000
FRAME_LEN = 400          #: 25 мс при 16 кГц
FRAME_SHIFT = 160        #: 10 мс
NFFT = 512               #: ближайшая степень двойки к длине окна
NUM_MEL = 80             #: столько полос ждёт модель на входе
LOW_FREQ = 20.0
PREEMPHASIS = 0.97

#: Сколько речи нужно, чтобы отпечаток вообще что-то значил.
#:
#: Имя намеренно НЕ `MIN_SPEECH_S`: так называется совсем другой порог —
#: `Config.min_speech_s` (`VERA_MIN_SPEECH_S`, 25с), сколько речи должно быть
#: во ВСЁМ разговоре, чтобы он вообще считался разговором. Здесь — про один
#: кусок и про отпечаток.
#:
#: Замер 04.09 на трёх голосах. Мерено так, как сравнивает кластеризация: два
#: КОРОТКИХ куска между собой, а не короткий с длинным образцом (второе льстит
#: и дало бы порог оптимистичнее реального):
#:
#:     длина   свои (min)   чужие (max)
#:      2.0с      0.537        0.421      ← ниже порога слияния
#:      2.5с      0.648        0.437      ← вровень с порогом
#:      3.0с      0.759        0.432
#:      3.5с      0.849        0.452
#:
#: Порог слияния 0.65 (`cluster.MERGE_THRESHOLD`) калиброван на кусках по пять
#: секунд. Куску короче трёх секунд он не по силам: свой голос сходится сам с
#: собой слабее, чем требует порог, и человек разъезжается на «нескольких».
#: Вживую 03.09 при пороге в 0.2с один собеседник дал девять «говорящих», и
#: из-за мнимой многоголосости перестало работать именование по заголовку
#: окна — разговор больше не считался один-на-один.
#:
#: Три секунды выбраны так, чтобы запас был с ОБЕИХ сторон: 0.759 против
#: порога 0.65 и 0.432 против него же. Двух секунд не хватало, двух с
#: половиной хватало вровень — а вровень уже подводило однажды.
#:
#: Цена — покрытие: в живом созвоне около 55% реплик собеседника длиннее трёх
#: секунд, остальные остаются без отпечатка. Размен осознанный: безымянная
#: реплика честнее приписанной наугад, и при одном голосе имя ей всё равно
#: достанется (`SpeakerSession.resolve`). В групповом созвоне короткие реплики
#: останутся безымянными — это известная грань, а не осечка.
MIN_VOICEPRINT_S = 3.0
MIN_FRAMES = 1 + (int(MIN_VOICEPRINT_S * SAMPLE_RATE) - FRAME_LEN) // FRAME_SHIFT


def povey_window(size: int) -> np.ndarray:
    """Окно Povey — симметричный ханн в степени 0.85, ровно как в kaldi.

    `np.hanning(size)`, а НЕ `np.hanning(size + 1)[:size]`. Разница — в
    знаменателе: у симметричного ханна это `size - 1`, у периодического
    `size`. Kaldi (`feature-window.cc`, `PoveyWindow`) берёт `frame_length-1`,
    то есть симметричный. Периодический расходится с ним до 0.006 по
    амплитуде — немного, но систематически по всему окну и на каждом кадре,
    а это ровно тот тихий дрейф признаков, от которого эмбеддинги слабеют
    молча. Нашло ревью; сверено с формулой kaldi — расхождение стало 0.0.
    """
    return np.hanning(size) ** 0.85


def _hz_to_mel(hz: np.ndarray | float) -> np.ndarray | float:
    return 1127.0 * np.log(1.0 + np.asarray(hz) / 700.0)


def _mel_to_hz(mel: np.ndarray | float) -> np.ndarray | float:
    return 700.0 * (np.exp(np.asarray(mel) / 1127.0) - 1.0)


def mel_filterbank(num_bins: int = NUM_MEL, nfft: int = NFFT,
                   sample_rate: int = SAMPLE_RATE,
                   low_freq: float = LOW_FREQ,
                   high_freq: float = 0.0) -> np.ndarray:
    """Треугольные мел-фильтры → [полосы, спектральные отсчёты].

    `high_freq=0` означает Найквиста — то же соглашение, что в kaldi.

    Треугольники строятся В МЕЛ-ШКАЛЕ, по каждому спектральному отсчёту, а не
    округлением границ до номеров отсчётов. Округление казалось проще, но
    внизу спектра мел-полосы уже расстояния между отсчётами, и соседние
    границы схлопывались в один номер: полосы 1 и 8 выходили ПУСТЫМИ —
    молча терялся кусок спектра, а признаки расходились с теми, на которых
    модель обучена. Поймано тестом `test_every_bin_has_weight`.
    """
    high = high_freq if high_freq > 0 else sample_rate / 2.0
    edges = np.linspace(_hz_to_mel(low_freq), _hz_to_mel(high), num_bins + 2)
    width = nfft // 2 + 1
    bin_mel = _hz_to_mel(np.arange(width) * sample_rate / nfft)

    banks = np.zeros((num_bins, width), dtype=np.float32)
    for i in range(num_bins):
        left, center, right = edges[i], edges[i + 1], edges[i + 2]
        rising = (bin_mel > left) & (bin_mel <= center)
        falling = (bin_mel > center) & (bin_mel < right)
        banks[i, rising] = (bin_mel[rising] - left) / (center - left)
        banks[i, falling] = (right - bin_mel[falling]) / (right - center)
    return banks


_BANKS = mel_filterbank()
_WINDOW = povey_window(FRAME_LEN)


def fbank(audio: np.ndarray) -> np.ndarray:
    """float32 в [-1, 1], моно 16 кГц → [кадры, 80] лог-мел с вычтенным средним.

    Возвращает пустой массив, если звука меньше одного окна — это не ошибка,
    а нормальный случай на обрывке речи; решение, что делать дальше,
    принимает вызывающий.
    """
    if audio.ndim != 1:
        raise ValueError(f"ждём моно-дорожку, пришло {audio.ndim} измерений")
    if len(audio) < FRAME_LEN:
        return np.zeros((0, NUM_MEL), dtype=np.float32)

    count = 1 + (len(audio) - FRAME_LEN) // FRAME_SHIFT
    index = np.arange(FRAME_LEN)[None, :] + FRAME_SHIFT * np.arange(count)[:, None]
    # kaldi считает в шкале int16, а не в [-1, 1]: масштаб влияет на логарифм.
    frames = audio[index].astype(np.float32) * 32768.0
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames = np.concatenate(
        [frames[:, :1] * (1.0 - PREEMPHASIS),
         frames[:, 1:] - PREEMPHASIS * frames[:, :-1]], axis=1)
    frames *= _WINDOW

    power = np.abs(np.fft.rfft(frames, n=NFFT)) ** 2
    mel = power @ _BANKS.T
    feats = np.log(np.maximum(mel, 1e-10)).astype(np.float32)
    # Вычитание среднего по времени (CMN): убирает вклад канала и громкости,
    # оставляя тембр. Без него отпечаток зависел бы от микрофона сильнее,
    # чем от голоса, и один человек в наушниках и без них стал бы двумя.
    return feats - feats.mean(axis=0, keepdims=True)
