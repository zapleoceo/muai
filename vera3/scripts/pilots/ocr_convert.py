"""PaddleOCR-VL → OpenVINO IR для пилота (docs/model-pilots.md).

Повторяет ноутбук openvinotoolkit/openvino_notebooks/notebooks/paddleocr_vl:
качает помощники ноутбука и веса с HF, подменяет modeling-файл патченой
версией и экспортирует IR (LLM int8, vision fp). Всё кладётся в --root
(по умолчанию D:\\pilots) — на C: места нет.

    set HF_HOME=D:\\pilots\\hf
    python ocr_convert.py --model PaddlePaddle/PaddleOCR-VL-1.6
"""
from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

_NB = ("https://raw.githubusercontent.com/openvinotoolkit/openvino_notebooks/"
       "latest/notebooks/paddleocr_vl/")
_HELPERS = ("ov_paddleocr_vl.py", "modeling_paddleocr_vl.py", "image_processing_paddleocr_vl.py")


def fetch_helpers(nb_dir: Path) -> None:
    nb_dir.mkdir(parents=True, exist_ok=True)
    for name in _HELPERS:
        if not (nb_dir / name).exists():
            (nb_dir / name).write_bytes(urllib.request.urlopen(_NB + name).read())


def ov_dir_for(root: Path, model_id: str, llm: str) -> Path:
    return root / "models" / f"ov_{model_id.split('/')[-1].lower().replace('.', '_')}_{llm}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="PaddlePaddle/PaddleOCR-VL-1.6")
    ap.add_argument("--root", type=Path, default=Path(r"D:\pilots"))
    ap.add_argument("--llm", choices=["int8", "int4"], default="int8")
    a = ap.parse_args()
    from huggingface_hub import snapshot_download

    nb_dir = a.root / "paddleocr_vl"
    fetch_helpers(nb_dir)
    pretrained = Path(snapshot_download(a.model, local_dir=str(a.root / "models" / a.model.replace("/", "__"))))
    # Ноутбук требует свою версию modeling-файла: оригинал не трассируется в IR.
    shutil.copy2(nb_dir / "modeling_paddleocr_vl.py", pretrained / "modeling_paddleocr_vl.py")
    out = ov_dir_for(a.root, a.model, a.llm)
    if out.exists():
        print(f"already converted: {out}")
        return
    sys.path.insert(0, str(nb_dir))
    from ov_paddleocr_vl import PaddleOCR_VL_OV

    conv = PaddleOCR_VL_OV(pretrained_model_path=str(pretrained), ov_model_path=str(out), device="CPU",
                           llm_int4_compress=a.llm == "int4", llm_int8_compress=a.llm == "int8",
                           vision_int8_quant=False)
    conv.export_paddleocr_vl_to_ov()
    conv.close()
    print(f"converted: {out}")


if __name__ == "__main__":
    main()
