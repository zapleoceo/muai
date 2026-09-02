"""Отпечаток голоса: кусок речи → вектор, по которому голоса сравнимы.

Модель — wespeaker ResNet34 (voxceleb, LM), ONNX на 25 МБ, считается через тот
же OpenVINO, что уже стоит ради whisper. Отдельного движка не заводим.

Замер на трёх голосах TTS (2026-09-02): свои пары 0.86–0.89 косинуса, чужие
0.16–0.50. Зазор 0.37 — с таким запасом порог можно ставить где угодно между
0.6 и 0.75, не балансируя на грани.

Считаем на CPU, а не на нейропроцессоре: модель в тридцать раз меньше whisper,
вызов занимает миллисекунды, а NPU занят распознаванием — вставать к нему в
очередь ради такой мелочи значило бы тормозить главное.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import numpy as np

from vera_listener.speakers.features import MIN_FRAMES, fbank

log = logging.getLogger("listener.speakers")

MODEL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
MODEL_FILE = "voxceleb_resnet34_LM.onnx"

#: Размерность отпечатка. Держим константой: на неё опираются хранилище
#: отпечатков и проверки в тестах, и молчаливая смена модели с другой
#: размерностью должна падать громко, а не портить сравнение.
EMBEDDING_DIM = 256


class SpeakerEmbedder(Protocol):
    """Что нужно остальному коду от опознавателя голоса.

    Ради этого протокола всё и разделено: сессия зависит от него, а не от
    OpenVINO, поэтому в тестах подставляется тривиальная реализация без
    модели на 25 МБ и без инференса.
    """

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        """Моно 16 кГц float32 → единичный вектор, или None если речи мало."""
        ...


class OpenVinoSpeakerEmbedder:
    """Реализация на OpenVINO. Модель грузится лениво — в тишине не нужна.

    НЕ потокобезопасен, и замка нет намеренно: единственный вызывающий —
    поток `stt` слушателя, он один на процесс (см. `app.py`, `_work`).
    Замок здесь был бы защитой от того, чего в конструкции нет; появится
    второй потребитель — замок понадобится, и об этом сказано тут, а не
    выяснится по двойной загрузке модели.
    """

    def __init__(self, model_dir: Path, device: str = "CPU"):
        self._model_dir = model_dir
        self._device = device
        self._compiled = None
        self._output = None

    def _model_path(self) -> Path:
        target = self._model_dir / MODEL_FILE
        if target.exists():
            return target
        from huggingface_hub import hf_hub_download

        log.info("качаю модель опознания голоса (%s, ~25 МБ, один раз)", MODEL_REPO)
        self._model_dir.mkdir(parents=True, exist_ok=True)
        hf_hub_download(MODEL_REPO, MODEL_FILE, local_dir=str(self._model_dir))
        if not target.exists():
            raise RuntimeError(f"модель {MODEL_FILE} не докачалась")
        return target

    def _load(self):
        if self._compiled is not None:
            return self._compiled
        import openvino as ov

        core = ov.Core()
        compiled = core.compile_model(core.read_model(self._model_path()), self._device)
        self._compiled, self._output = compiled, compiled.output(0)
        log.info("опознание голоса готово (%s, %s)", MODEL_REPO, self._device)
        return compiled

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        feats = fbank(audio)
        if feats.shape[0] < MIN_FRAMES:
            return None
        compiled = self._load()
        vector = normalize(np.asarray(
            compiled({"feats": feats[None, ...]})[self._output][0],
            dtype=np.float32))
        # Нулевой вектор не отпечаток, а его отсутствие: сравнение с ним
        # даёт ноль похожести с кем угодно, и он тихо осел бы отдельным
        # «говорящим». Наружу отдаём то же, что и на слишком коротком куске.
        return None if not float(np.linalg.norm(vector)) else vector


def normalize(vector: np.ndarray) -> np.ndarray:
    """К единичной длине — тогда скалярное произведение и есть косинус.

    Нулевой вектор оставляем нулевым: делить на ноль нельзя, а тихо получить
    вектор из NaN хуже, чем честный ноль, который сравнение отвергнет.
    """
    norm = float(np.linalg.norm(vector))
    return vector if norm == 0.0 else (vector / norm).astype(np.float32)


def similarity(left: np.ndarray, right: np.ndarray) -> float:
    """Косинус между отпечатками: 1.0 — тот же голос, 0.0 — ничего общего."""
    return float(np.dot(normalize(left), normalize(right)))
