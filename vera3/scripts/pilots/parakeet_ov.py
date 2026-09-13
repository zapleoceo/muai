"""parakeet-tdt-0.6b-v3 на OpenVINO (сборка FluidInference/parakeet-tdt-0.6b-v3-ov).

Четыре графа: мел-спектр → энкодер → LSTM-предсказатель → joint. Графы
статические: окно 15 с (240 000 отсчётов → 188 кадров энкодера), поэтому
длинное голосовое режется на куски. Резать ровно по 15 с нельзя — слово
рвётся пополам; режем в самой тихой точке последних 3 с окна.

Состояние LSTM между кусками НЕ переносим, хотя eddy так делает: замер
14.09.2026 на 37-секундном голосовом — с переносом второй кусок молча
терял первые ~8 с речи (декодер уходил в пустые токены после точки),
а с чистым состоянием на каждый кусок текст полный. Режем по паузе,
так что потеря контекста между кусками — минимальная.

Жадный TDT-декодер повторяет eddy/src/models/parakeet-v2/parakeet_decoder.cpp.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import openvino as ov

SR = 16_000
WINDOW = 240_000
BLANK = 8192
DURATIONS = (0, 1, 2, 3, 4)
_MAX_SYMBOLS_PER_FRAME = 10


def split_points(n: int, audio: np.ndarray, window: int = WINDOW, search: int = 3 * SR) -> list[tuple[int, int]]:
    spans, start, hop = [], 0, SR // 50
    while n - start > window:
        lo, hi = start + window - search, start + window
        seg = audio[lo:hi]
        energy = np.convolve(seg ** 2, np.ones(hop * 10), mode="valid")
        cut = lo + int(np.argmin(energy[::hop]) * hop) + hop * 5
        spans.append((start, cut))
        start = cut
    spans.append((start, n))
    return spans


class Parakeet:
    def __init__(self, model_dir: Path, device: str, cache_dir: Path | None = None) -> None:
        core = ov.Core()
        if cache_dir:
            core.set_property({"CACHE_DIR": str(cache_dir)})
        # Мел-спектр и маленькие decoder/joint держим на CPU: их тысячи мелких
        # вызовов, и пересылка на ускоритель дороже самого счёта. На device
        # идёт только энкодер — в нём весь вес модели.
        self.mel = core.compile_model(model_dir / "parakeet_melspectogram.xml", "CPU").create_infer_request()
        self.enc = core.compile_model(model_dir / "parakeet_encoder.xml", device).create_infer_request()
        self.dec = core.compile_model(model_dir / "parakeet_decoder.xml", "CPU").create_infer_request()
        self.joint = core.compile_model(model_dir / "parakeet_joint.xml", "CPU").create_infer_request()
        vocab = json.loads((model_dir / "parakeet_v3_vocab.json").read_text(encoding="utf-8"))
        self.vocab = {int(k): v for k, v in vocab.items()}

    def _encode(self, chunk: np.ndarray) -> np.ndarray:
        sig = np.zeros((1, WINDOW), dtype=np.float32)
        sig[0, : len(chunk)] = chunk
        self.mel.infer({"input_signals": sig, "input_length": np.array([len(chunk)], dtype=np.int64)})
        mel, mel_len = self.mel.get_output_tensor(0).data.copy(), self.mel.get_output_tensor(1).data
        self.enc.infer({"melspectogram": mel, "melspectogram_length": mel_len.astype(np.int32)})
        out = self.enc.get_output_tensor(0).data
        valid = int(self.enc.get_output_tensor(1).data[0])
        return out[0, :, :valid].T.copy()

    def _decode(self, frames: np.ndarray, state: dict) -> list[int]:
        tokens: list[int] = []
        t, emitted_here = 0, 0
        dec_out = None
        while t < len(frames):
            if dec_out is None:
                self.dec.infer({"targets": np.array([[state["last"]]], dtype=np.int64),
                                "h_in": state["h"], "c_in": state["c"]})
                dec_out = self.dec.get_output_tensor(0).data.copy()
                nh, nc = self.dec.get_output_tensor(1).data.copy(), self.dec.get_output_tensor(2).data.copy()
            self.joint.infer({"encoder_outputs": frames[t][None, None, :], "decoder_outputs": dec_out})
            logits = self.joint.get_output_tensor(0).data[0, 0, 0]
            tok = int(np.argmax(logits[: BLANK + 1]))
            dur = DURATIONS[int(np.argmax(logits[BLANK + 1:]))]
            if tok != BLANK:
                tokens.append(tok)
                state.update(last=tok, h=nh, c=nc)
                dec_out = None
                emitted_here += 1
            if dur == 0 and (tok == BLANK or emitted_here >= _MAX_SYMBOLS_PER_FRAME):
                dur = 1
            if dur:
                t += dur
                emitted_here = 0
        return tokens

    def transcribe(self, audio: np.ndarray) -> str:
        tokens: list[int] = []
        for a, b in split_points(len(audio), audio):
            state = {"last": BLANK, "h": np.zeros((2, 1, 640), np.float32),
                     "c": np.zeros((2, 1, 640), np.float32)}
            tokens += self._decode(self._encode(audio[a:b]), state)
        text = "".join(self.vocab.get(t, "") for t in tokens)
        return text.replace("▁", " ").strip()
