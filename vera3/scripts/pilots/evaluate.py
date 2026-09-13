"""Сводка пилотов по results/*.jsonl — только агрегаты, без текстов.

    python evaluate.py photo
    python evaluate.py voice
    python evaluate.py voice --disagreements 5   # спорные случаи, печать локально
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import mean, median

from metrics import char_similarity, gemini_ocr_part, number_recall, token_recall, wer

ROOT = Path(r"D:\pilots")


def _load(path: Path) -> dict[int, dict]:
    return {r["event_id"]: r for r in map(json.loads, path.open(encoding="utf-8"))}


def _manifest(kind: str) -> dict[int, dict]:
    return _load(ROOT / "data" / kind / "manifest.jsonl")


def looped(text: str) -> bool:
    # Зацикливание декодера («1\n2\n3…» до лимита токенов) — видно по доле
    # повторяющихся строк; живой документ так не выглядит.
    lines = [x for x in text.splitlines() if x.strip()]
    return len(lines) >= 30 and Counter(lines).most_common(1)[0][1] >= 10 or len(set(lines)) < len(lines) / 3


def photo_report() -> None:
    ref = _manifest("photo")
    for path in sorted((ROOT / "results").glob("*.jsonl")):
        if not path.name.startswith(("ocr_", "vlm_")):
            continue
        rows = _load(path)
        if not rows:
            continue
        by_group: dict[str, list[tuple[float, float, float | None]]] = {}
        for eid, r in rows.items():
            gold = gemini_ocr_part(ref[eid]["ref"])
            hyp = gemini_ocr_part(r["text"]) if path.name.startswith("vlm_") else r["text"]
            by_group.setdefault(r["group"], []).append(
                (char_similarity(hyp, gold), token_recall(hyp, gold), number_recall(hyp, gold)))
        summary = json.loads(path.with_suffix(".summary.json").read_text()) if path.with_suffix(
            ".summary.json").exists() else {}
        print(f"\n{path.stem}: n={len(rows)} time median={median(r['time_s'] for r in rows.values()):.1f}s "
              f"max={max(r['time_s'] for r in rows.values()):.1f}s peak={summary.get('peak_wset_mb', 0):.0f}MB "
              f"loops={sum(looped(r['text']) for r in rows.values())} "
              f"len median={median(len(r['text']) for r in rows.values())}")
        for g, vals in sorted(by_group.items()) + [("ALL", [v for vs in by_group.values() for v in vs])]:
            nums = [v[2] for v in vals if v[2] is not None]
            print(f"  {g:6} n={len(vals):2} char_sim={median(v[0] for v in vals):.2f} "
                  f"tok_recall={median(v[1] for v in vals):.2f} "
                  f"num_recall={mean(nums) if nums else float('nan'):.2f} (n={len(nums)})")


def voice_report(disagreements: int) -> None:
    ref = _manifest("voice")
    runs = {p.stem: _load(p) for p in sorted((ROOT / "results").glob("asr_*.jsonl"))
            if p.with_suffix(".summary.json").exists()}
    for name, rows in runs.items():
        summary = json.loads((ROOT / "results" / f"{name}.summary.json").read_text())
        warm = [r for r in rows.values() if r["warm"]]
        rtf = sum(r["audio_s"] for r in warm) / sum(r["time_s"] for r in warm)
        print(f"\n{name}: n={len(rows)} audio={sum(r['audio_s'] for r in rows.values()):.0f}s "
              f"×real(warm)={rtf:.1f} load={summary['load_s']:.1f}s peak={summary['peak_wset_mb']:.0f}MB "
              f"cpu_s={summary['cpu_s']:.0f}")
        for g in ("ru", "uk"):
            ws = [wer(r["text"], ref[e]["ref"]) for e, r in rows.items() if r["group"] == g]
            print(f"  {g}: WER vs prod median={median(ws):.2f} mean={mean(ws):.2f}")
    # Сравниваем боевую конфигурацию слушателя: оба на NPU, язык по группе.
    par, wh = runs.get("asr_parakeet_NPU_group"), runs.get("asr_whisper_NPU_group")
    if not (par and wh):
        return
    pairs = sorted(((wer(par[e]["text"], wh[e]["text"]), e) for e in par if e in wh), reverse=True)
    for g in ("ru", "uk"):
        ws = [w for w, e in pairs if par[e]["group"] == g]
        print(f"parakeet vs whisper-local {g}: WER median={median(ws):.2f}")
    for w, e in pairs[:disagreements]:
        print(f"\n--- {e} ({par[e]['group']}) parakeet↔whisper WER={w:.2f}\nPROD: {ref[e]['ref']}\n"
              f"WHISPER: {wh[e]['text']}\nPARAKEET: {par[e]['text']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["photo", "voice"])
    ap.add_argument("--disagreements", type=int, default=0)
    a = ap.parse_args()
    if a.kind == "photo":
        photo_report()
    else:
        voice_report(a.disagreements)


if __name__ == "__main__":
    main()
