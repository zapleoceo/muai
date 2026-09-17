"""Журнал векторов прошедших разговоров — чтобы калибровать на своих записях.

Пороги опознания калибровались дважды на синтезированных голосах и дважды не
выдержали живого созвона: 04.09 — порог длины, 16.09 — обрезка тишины. Причина
одна и та же: у настоящей речи через кодек вся шкала сходств едет вниз, и
цифры, снятые на чистом синтезе, к ней не относятся.

Починить это подкруткой порога нельзя — нужны настоящие векторы настоящих
разговоров. Здесь они и копятся.

**Звука тут нет и не будет.** Вектор — 256 чисел, речь по нему не
восстанавливается. 1 КБ на реплику, около 150 КБ на разговор. Каталог
локальный, в очередь отправки не попадает, наружу не уходит ничего.

Старые разговоры вытесняются: диагностика не имеет права расти бесконечно на
чужом диске.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger("listener.speakers")

#: Сколько разговоров держим. Пятьдесят — это недели две обычной работы и
#: около 8 МБ: достаточно, чтобы посчитать распределение сходств, и мало,
#: чтобы это кого-то беспокоило.
KEEP_SESSIONS = 50


def write(directory: Path, embeddings: list[np.ndarray], keys: list[float],
          names: list[str], members: list[list[int]], *,
          counterpart: str | None, app: str | None) -> None:
    """Сложить векторы разговора и то, как их разметили.

    Сбой записи журнала не имеет права портить разговор: это диагностика, а
    не работа, и текст с именами уже готов. Поэтому ошибки гасятся.
    """
    if not embeddings:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        label = {index: names[cluster]
                 for cluster, group in enumerate(members) for index in group}
        meta = {
            "at": stamp,
            "app": app,
            "counterpart": counterpart,
            "voices": names,
            "lines": [{"at": keys[i], "voice": label.get(i)}
                      for i in range(len(embeddings))],
        }
        # Векторы ПЕРВЫМИ, разметку вторыми. Если запись оборвётся между
        # ними, на диске останется `.npy` — а его вытеснение видит. В обратном
        # порядке остался бы `.json`, который старая чистка не находила вовсе
        # и который жил бы вечно, унося с собой имена. Нашло ревью.
        np.save(directory / f"{stamp}.npy", np.stack(embeddings))
        (directory / f"{stamp}.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        _forget_old(directory)
    except Exception as e:                              # noqa: BLE001
        log.warning("журнал векторов не записался (%s)", type(e).__name__)


def _forget_old(directory: Path) -> None:
    """Оставить последние `KEEP_SESSIONS` разговоров, остальные удалить.

    Считаем по ОБОИМ расширениям, а не по одним векторам: половинка от
    оборванной записи — тоже разговор, и она тоже обязана вытесняться.
    """
    stamps = sorted({p.stem for p in directory.glob("*.npy")}
                    | {p.stem for p in directory.glob("*.json")})
    for stamp in stamps[:-KEEP_SESSIONS]:
        (directory / f"{stamp}.npy").unlink(missing_ok=True)
        (directory / f"{stamp}.json").unlink(missing_ok=True)
