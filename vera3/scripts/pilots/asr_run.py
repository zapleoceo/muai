"""Прогон выборки голосовых через одну ASR-модель на одном устройстве.

Один процесс = одна модель/устройство, чтобы пиковая память (peak working
set) относилась только к ней.

    python asr_run.py parakeet NPU
    python asr_run.py whisper NPU --language auto
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import numpy as np
import psutil

ROOT = Path(r"D:\pilots")
WHISPER_DIR = Path(r"D:\vera-listener\models\OpenVINO__whisper-large-v3-turbo-int8-ov")


def load_audio(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                          "-ar", "16000", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def build(engine: str, device: str, language: str):  # noqa: ANN201 — разные движки
    cache = ROOT / "ovcache"
    if engine == "parakeet":
        from parakeet_ov import Parakeet

        m = Parakeet(ROOT / "models" / "parakeet-v3", device, cache)
        return lambda audio, _lang: m.transcribe(audio)
    import openvino_genai as ov_genai

    pipe = ov_genai.WhisperPipeline(str(WHISPER_DIR), device=device, CACHE_DIR=str(cache))

    def run(audio: np.ndarray, lang: str) -> str:
        kw = {"task": "transcribe", "return_timestamps": True}
        if language != "auto":
            kw["language"] = f"<|{lang if language == 'group' else language}|>"
        return str(pipe.generate(audio.tolist(), **kw))
    return run


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("engine", choices=["parakeet", "whisper"])
    ap.add_argument("device", choices=["CPU", "GPU", "NPU"])
    ap.add_argument("--language", default="group", help="group|auto|ru|uk (только whisper)")
    a = ap.parse_args()
    data = ROOT / "data" / "voice"
    rows = [json.loads(line) for line in (data / "manifest.jsonl").open(encoding="utf-8")]
    t0 = time.perf_counter()
    model = build(a.engine, a.device, a.language)
    load_s = time.perf_counter() - t0
    proc = psutil.Process()
    out_path = ROOT / "results" / f"asr_{a.engine}_{a.device}_{a.language}.jsonl"
    out_path.parent.mkdir(exist_ok=True)
    cpu0 = sum(proc.cpu_times()[:2])
    with out_path.open("w", encoding="utf-8") as f:
        for i, r in enumerate(rows):
            audio = load_audio(data / r["file"])
            t = time.perf_counter()
            text = model(audio, r["group"])
            took = time.perf_counter() - t
            f.write(json.dumps({"event_id": r["event_id"], "group": r["group"], "audio_s": len(audio) / 16000,
                                "time_s": took, "text": text, "warm": i > 0}, ensure_ascii=False) + "\n")
    mem = proc.memory_info()
    summary = {"engine": a.engine, "device": a.device, "language": a.language, "load_s": load_s,
               "peak_wset_mb": getattr(mem, "peak_wset", mem.rss) / 2**20,
               "cpu_s": sum(proc.cpu_times()[:2]) - cpu0, "files": len(rows)}
    (out_path.with_suffix(".summary.json")).write_text(json.dumps(summary), encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
