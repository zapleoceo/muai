"""Отпечатки голосов между разговорами: имя ↔ центроид.

Смысл всей затеи: звонок один на один в Slack или Telegram называет
собеседника прямо в заголовке окна. Значит голос из такого разговора можно
запомнить с именем — и потом узнать того же человека в общем созвоне Meet,
где имён не даёт никто.

Порог узнавания ВЫШЕ порога слияния внутри разговора: приписать чужую реплику
живому человеку по имени хуже, чем оставить её безымянной. Ошибку первого
рода видно как ложь, ошибку второго — просто как неполноту.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from vera_listener.speakers.embedder import EMBEDDING_DIM, normalize

log = logging.getLogger("listener.speakers")

#: Порог узнавания знакомого голоса. Строже, чем MERGE_THRESHOLD (0.65).
RECOGNITION_THRESHOLD = 0.72

#: Сколько разговоров усредняем в отпечаток. Дальше вклад нового разговора
#: затухает: голос человека устойчив, а вот запись — нет (гарнитура, комната,
#: связь), и свежая плохая запись не должна сдвигать накопленный отпечаток.
MAX_WEIGHT = 20


@dataclass
class Voiceprint:
    name: str
    centroid: np.ndarray
    weight: int = 1

    def to_json(self) -> dict:
        return {"name": self.name,
                "centroid": [round(float(x), 6) for x in self.centroid],
                "weight": self.weight}

    @staticmethod
    def from_json(raw: dict) -> Voiceprint | None:
        centroid = np.asarray(raw.get("centroid") or [], dtype=np.float32)
        if centroid.shape != (EMBEDDING_DIM,) or not raw.get("name"):
            return None
        return Voiceprint(name=str(raw["name"]), centroid=normalize(centroid),
                          weight=max(1, int(raw.get("weight") or 1)))


class VoiceprintRegistry:
    """Хранилище отпечатков на диске. Порча файла не роняет слушателя.

    НЕ потокобезопасно, и замка нет намеренно — по той же причине, что и у
    `OpenVinoSpeakerEmbedder`: единственный вызывающий это поток `stt`
    слушателя, он один на процесс. Появится второй — понадобится замок, и
    узнать об этом лучше отсюда, чем по затёртому файлу отпечатков.
    """

    def __init__(self, path: Path, *, threshold: float = RECOGNITION_THRESHOLD):
        self.path = path
        self.threshold = threshold
        self._prints: list[Voiceprint] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # Отпечатки — удобство, а не данные разговора: испорченный файл
            # не повод отказываться писать разговоры. Начинаем с пустого.
            log.warning("отпечатки голосов не прочитались (%s) — начинаю заново", e)
            return
        self._prints = [p for p in (Voiceprint.from_json(r) for r in raw) if p]

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps([p.to_json() for p in self._prints], ensure_ascii=False),
                encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("не сохранил отпечатки голосов: %s", e)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self._prints]

    def match(self, centroid: np.ndarray) -> str | None:
        """Знакомый голос → имя. Никого похожего → None."""
        best_name, best_score = None, self.threshold
        vector = normalize(np.asarray(centroid, dtype=np.float32))
        for print_ in self._prints:
            score = float(np.dot(vector, print_.centroid))
            if score >= best_score:
                best_name, best_score = print_.name, score
        return best_name

    def remember(self, name: str, centroid: np.ndarray) -> None:
        """Запомнить голос под именем; знакомого — уточнить, не перезаписать."""
        vector = normalize(np.asarray(centroid, dtype=np.float32))
        if not name or float(np.linalg.norm(vector)) == 0.0:
            return
        for print_ in self._prints:
            if print_.name == name:
                weight = min(print_.weight, MAX_WEIGHT)
                blended = print_.centroid * weight + vector
                print_.centroid = normalize(blended)
                print_.weight = min(weight + 1, MAX_WEIGHT)
                return
        self._prints.append(Voiceprint(name=name, centroid=vector))
        log.info("запомнил голос: %s", name)
