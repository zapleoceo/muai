"""Отчёт apply/undo на диске: запись целиком или никак (tmp + fsync + replace)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=1))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
