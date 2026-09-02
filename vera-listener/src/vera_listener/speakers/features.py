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

#: Короче этого fbank бессмысленен: на одном-двух кадрах модель даёт шум,
#: а не отпечаток голоса. Порог держим здесь, а не у вызывающего, чтобы он
#: не разъехался между местами использования.
MIN_FRAMES = 20


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
