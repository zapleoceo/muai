"""Прогон выборки фото через PaddleOCR-VL (OpenVINO IR из ocr_convert.py).

Один процесс = одно устройство, чтобы пиковая память относилась к нему.
Задача «OCR:» на целой картинке, без детектора раскладки PP-DocLayout —
именно так модель встала бы в media-worker вместо vision-LLM.

    python ocr_run.py CPU
    python ocr_run.py GPU --limit 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(r"D:\pilots")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("device", choices=["CPU", "GPU", "NPU"])
    ap.add_argument("--model-dir", type=Path, default=ROOT / "models" / "ov_paddleocr-vl-1_6_int8")
    ap.add_argument("--llm", choices=["int8", "int4"], default="int8")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-new-tokens", type=int, default=1024)
    ap.add_argument("--threads", type=int, default=0, help="CPU: потолок потоков (2 = как на сервере)")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT / "paddleocr_vl"))
    import openvino as ov
    from ov_paddleocr_vl import OVPaddleOCRVLForCausalLM
    from PIL import Image

    core = ov.Core()
    if a.threads:
        core.set_property("CPU", {"INFERENCE_NUM_THREADS": a.threads})
    t0 = time.perf_counter()
    model = OVPaddleOCRVLForCausalLM(core=core, ov_model_path=str(a.model_dir), device=a.device,
                                     llm_int4_compress=a.llm == "int4", llm_int8_compress=a.llm == "int8",
                                     llm_int8_quant=a.device != "NPU", llm_infer_list=[], vision_infer=[])
    load_s = time.perf_counter() - t0
    tok = model.tokenizer
    gen = {"bos_token_id": tok.bos_token_id, "eos_token_id": tok.eos_token_id, "pad_token_id": tok.pad_token_id,
           "max_new_tokens": a.max_new_tokens, "do_sample": False}
    data = ROOT / "data" / "photo"
    rows = [json.loads(line) for line in (data / "manifest.jsonl").open(encoding="utf-8")]
    rows = rows[: a.limit] if a.limit else rows
    out_path = ROOT / "results" / f"ocr_paddle_{a.device}_{a.llm}_t{a.threads}.jsonl"
    out_path.parent.mkdir(exist_ok=True)
    proc = psutil.Process()
    cpu0 = sum(proc.cpu_times()[:2])
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            img = Image.open(data / r["file"]).convert("RGB")
            msgs = [{"role": "user", "content": [{"type": "image", "image": img}, {"type": "text", "text": "OCR:"}]}]
            t = time.perf_counter()
            text, _ = model.chat(messages=msgs, generation_config=gen)
            took = time.perf_counter() - t
            f.write(json.dumps({"event_id": r["event_id"], "group": r["group"], "time_s": took,
                                "size": img.size, "text": text}, ensure_ascii=False) + "\n")
            f.flush()
            print(r["event_id"], f"{took:.1f}s", len(text), flush=True)
    mem = proc.memory_info()
    summary = {"engine": "paddleocr-vl-1.6", "device": a.device, "llm": a.llm, "threads": a.threads, "load_s": load_s,
               "peak_wset_mb": getattr(mem, "peak_wset", mem.rss) / 2**20,
               "cpu_s": sum(proc.cpu_times()[:2]) - cpu0, "files": len(rows)}
    out_path.with_suffix(".summary.json").write_text(json.dumps(summary), encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
