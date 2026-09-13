"""Прогон выборки фото через qwen3-vl-4b (OpenVINO/Qwen3-VL-4B-Instruct-int4-ov).

Промпт — дословно боевой media_worker.recognize._VISION_PROMPT, max_tokens=400
и temperature≈0 как в проде, чтобы сравнение с gemini было честным.

    python vlm_run.py GPU
    python vlm_run.py CPU --threads 2 --limit 5
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import psutil

ROOT = Path(r"D:\pilots")
PROMPT = (
    "Опиши изображение по-русски в 1-3 коротких предложениях. "
    "Если на нём есть читаемый текст — приведи его дословно после метки `Текст:`. "
    "Если это скриншот UI/таблицы/чата — назови ключевые элементы (имена, числа, дата). "
    "Не выдумывай детали, которых не видно."
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("device", choices=["CPU", "GPU", "NPU"])
    ap.add_argument("--model-dir", type=Path, default=ROOT / "models" / "OpenVINO__Qwen3-VL-4B-Instruct-int4-ov")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    import openvino as ov
    import openvino_genai as ov_genai
    from PIL import Image

    cfg: dict = {"CACHE_DIR": str(ROOT / "ovcache")}
    if a.threads:
        cfg["INFERENCE_NUM_THREADS"] = a.threads
    t0 = time.perf_counter()
    pipe = ov_genai.VLMPipeline(str(a.model_dir), a.device, **cfg)
    load_s = time.perf_counter() - t0
    gen = ov_genai.GenerationConfig()
    gen.max_new_tokens = 400
    data = ROOT / "data" / "photo"
    rows = [json.loads(line) for line in (data / "manifest.jsonl").open(encoding="utf-8")]
    rows = rows[: a.limit] if a.limit else rows
    out_path = ROOT / "results" / f"vlm_qwen3vl4b_{a.device}_t{a.threads}.jsonl"
    out_path.parent.mkdir(exist_ok=True)
    proc = psutil.Process()
    cpu0 = sum(proc.cpu_times()[:2])
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            img = Image.open(data / r["file"]).convert("RGB")
            # 14.09.2026: фото 1928×2560 на CPU роняло qwen по памяти («Failed to
            # allocate» на ~6k визуальных токенах) — ужимаем длинную сторону до 1280,
            # как у обычного телеграм-фото.
            img.thumbnail((1280, 1280))
            tensor = ov.Tensor(np.array(img, dtype=np.uint8)[None])
            t = time.perf_counter()
            text = str(pipe.generate(PROMPT, images=[tensor], generation_config=gen))
            took = time.perf_counter() - t
            f.write(json.dumps({"event_id": r["event_id"], "group": r["group"], "time_s": took,
                                "size": img.size, "text": text}, ensure_ascii=False) + "\n")
            f.flush()
            print(r["event_id"], f"{took:.1f}s", len(text), flush=True)
    mem = proc.memory_info()
    summary = {"engine": "qwen3-vl-4b-int4", "device": a.device, "threads": a.threads, "load_s": load_s,
               "peak_wset_mb": getattr(mem, "peak_wset", mem.rss) / 2**20,
               "cpu_s": sum(proc.cpu_times()[:2]) - cpu0, "files": len(rows)}
    out_path.with_suffix(".summary.json").write_text(json.dumps(summary), encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
