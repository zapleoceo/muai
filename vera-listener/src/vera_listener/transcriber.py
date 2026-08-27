"""Локальное распознавание на нейропроцессоре (OpenVINO GenAI, whisper).

Модель грузится лениво и живёт до конца процесса: в тишине она не нужна.
Распознаём кусками по ходу разговора, а не одним пакетом в конце — иначе
часовой созвон целиком висел бы в оперативке и терялся при падении.

## Почему OpenVINO, а не faster-whisper

Замер на Acer Swift SFG14-75 (Core Ultra 7 258V), 100.3 с русской речи, одна
и та же модель `whisper-small` int8:

| движок / устройство      | время | ×реального | съедено CPU | % ядра |
|--------------------------|-------|-----------|-------------|--------|
| faster-whisper, CPU      | 10.1с | 9.9×      | 19.1с       | 189%   |
| OpenVINO, CPU            |  6.5с | 15.4×     | 20.2с       | 312%   |
| OpenVINO, GPU (Arc 140V) |  3.1с | 32×       |  2.9с       |  94%   |
| OpenVINO, NPU (AI Boost) |  3.8с | 26.7×     |  0.8с       |  22%   |

`faster-whisper` работает на CTranslate2, а тот умеет только CPU и CUDA —
нейропроцессора и Arc для него не существует. Отсюда замена движка.

Освободившийся процессор вложен в модель крупнее: `large-v3-turbo` int8 на NPU
даёт 4.3 с (×23.5) при тех же 22% ядра — то есть модель в шесть раз больше
нынешней работает в 2.3 раза быстрее и жрёт в 19 раз меньше процессора. На том
же образце small написал «Виранда», turbo — «Веранда» и «отчёт».

NPU, а не GPU, хотя GPU быстрее: слушатель работает в фоне сутками, и 22% ядра
против 98% важнее лишних секунд, а Arc остаётся свободным под работу владельца.

## Кэш компиляции обязателен

Первая компиляция turbo под NPU заняла 141 с (small — 37 с). С `CACHE_DIR`
повторный старт — 1.4 с. Без кэша слушатель вставал бы на две минуты при каждом
запуске, а он стартует на каждом логоне и после каждого падения. Обновление
драйвера кэш обесценивает — тогда одна медленная компиляция повторится, и об
этом честно пишется в лог, иначе выглядит как повисание.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

from vera_listener.config import Config

log = logging.getLogger("listener.stt")

SAMPLE_RATE = 16_000

#: Короче этого распознавать нечего: на обрывке в 64 мс whisper выдаёт либо
#: пустоту, либо выдумку («Спасибо за просмотр»). В логе 2026-08-27 таких
#: запусков было шесть за два часа — все на закрытии пустых сессий.
MIN_AUDIO_S = 0.6


#: Минимум, без которого пайплайн не соберётся. Проверять по ОДНОМУ файлу
#: нельзя: если загрузка оборвалась (нет сети, кончилось место) после
#: энкодера и до декодера, код решил бы «модель есть», пайплайн падал бы на
#: каждом старте, и так до ручного удаления каталога. Нашло ревью.
REQUIRED_FILES = (
    "openvino_encoder_model.xml", "openvino_encoder_model.bin",
    "openvino_decoder_model.xml", "openvino_decoder_model.bin",
    "config.json", "generation_config.json",
)


def model_is_complete(target: Path) -> bool:
    """Все ли файлы модели на месте и не осталось ли обрывков загрузки.

    `huggingface_hub` докачивает во временные файлы с суффиксом `.incomplete` —
    их наличие и есть прямой признак, что качали, но не докачали.
    """
    if not target.is_dir():
        return False
    if any(target.rglob("*.incomplete")):
        return False
    return all((target / name).exists() for name in REQUIRED_FILES)


def device_chain(preferred: str) -> list[str]:
    """Устройства по порядку попыток. CPU в конце всегда.

    На другом ноутбуке нейропроцессора может не быть вовсе, и слушатель обязан
    там работать — просто дороже. Молча падать на «нет NPU» он не имеет права.
    """
    chain = [preferred.upper()]
    if "CPU" not in chain:
        chain.append("CPU")
    return chain


class Transcriber:
    def __init__(self, config: Config):
        self.config = config
        self._pipe = None
        self._device: str | None = None
        # Устройства, отвалившиеся УЖЕ В РАБОТЕ. Второй раз туда не идём: иначе
        # каждый кусок платил бы компиляцией и падением по кругу.
        self._banned: set[str] = set()

    @property
    def device(self) -> str | None:
        """На чём реально считаем. None — модель ещё не грузили."""
        return self._device

    def warm_up(self) -> str:
        """Скачать и скомпилировать заранее. Возвращает устройство.

        Публичный вход для `--warmup` и тестов: раньше они дёргали `_load()`,
        то есть приватное имя, которое переименовали бы при первом рефакторинге.
        """
        self._load()
        return self._device or "?"

    def _model_dir(self) -> Path:
        """Локальная копия модели. Качаем один раз, дальше живём офлайн."""
        target = self.config.model_dir / self.config.model_id.replace("/", "__")
        if model_is_complete(target):
            return target
        from huggingface_hub import snapshot_download

        log.info("качаю модель %s (это один раз, ~790 МБ)", self.config.model_id)
        snapshot_download(self.config.model_id, local_dir=str(target))
        if not model_is_complete(target):
            raise RuntimeError(f"модель {self.config.model_id} докачалась не полностью")
        return target

    def _load(self):
        if self._pipe is not None:
            return self._pipe
        import openvino_genai as ov_genai

        model_dir = self._model_dir()
        cache = self.config.model_dir / "ovcache"
        cache.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        for device in device_chain(self.config.stt_device):
            if device in self._banned:
                continue
            started = time.monotonic()
            try:
                pipe = ov_genai.WhisperPipeline(str(model_dir), device=device,
                                                CACHE_DIR=str(cache))
            except Exception as e:                      # noqa: BLE001
                # Не «нет устройства» — так же выглядит устаревший драйвер или
                # занятая память. Причину сохраняем и идём дальше, а не падаем.
                errors.append(f"{device}: {type(e).__name__}: {e}")
                log.warning("%s не подошёл (%s) — пробую следующее устройство",
                            device, type(e).__name__)
                continue
            took = time.monotonic() - started
            if took > 20:
                log.info("компиляция под %s заняла %.0fс — это кэшируется, "
                         "следующий старт будет быстрым", device, took)
            log.info("распознавание на %s, модель %s (%.1fс)", device,
                     self.config.model_id, took)
            self._pipe, self._device = pipe, device
            return pipe
        raise RuntimeError("ни одно устройство не поднялось: " + "; ".join(errors))

    def transcribe(self, pcm: bytes) -> list[tuple[float, str]]:
        """PCM16 16 кГц → [(смещение от начала куска, текст)]."""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if len(audio) < MIN_AUDIO_S * SAMPLE_RATE:
            return []
        try:
            result = self._load().generate(
                audio,
                language=f"<|{self.config.language}|>",
                task="transcribe",
                return_timestamps=True,
            )
        except Exception:                              # noqa: BLE001
            # Устройство могло отвалиться уже в работе: драйвер откатился, NPU
            # занят, память кончилась. Пайплайн кэширован, поэтому без сброса
            # ВСЕ дальнейшие куски терялись бы до перезапуска процесса, а он
            # живёт сутками — то есть распознавание умирало бы молча. Нашло
            # ревью. Сбрасываем, помечаем устройство негодным и пробрасываем:
            # этот кусок потерян, следующий поедет на CPU.
            failed, self._pipe, self._device = self._device, None, None
            if failed:
                self._banned.add(failed)
                log.warning("%s отвалился в работе — дальше без него", failed)
            raise
        return segments_of(result)


def segments_of(result) -> list[tuple[float, str]]:
    """Ответ пайплайна → [(смещение, текст)]. Пустые реплики выброшены.

    Таймкоды нужны буквально: смещение внутри куска складывается со смещением
    куска в сессии, и по этой сумме реплики выстраиваются в хронологию. Если
    `chunks` не пришли, отдавать текст без времени нельзя — он ляжет в начало
    разговора и перепутает порядок.
    """
    chunks = getattr(result, "chunks", None)
    if not chunks:
        text = str(result).strip()
        if text:
            log.warning("таймкодов нет — реплика уходит с нулевым смещением")
            return [(0.0, text)]
        return []
    out: list[tuple[float, str]] = []
    for chunk in chunks:
        text = (chunk.text or "").strip()
        if text:
            out.append((float(chunk.start_ts), text))
    return out
