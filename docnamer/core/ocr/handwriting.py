"""Handwriting recognition.

* CtcOnnxRecognizer - any CRNN/CTC ONNX model (PaddleOCR-style). Gives TRUE per-character boxes
  and confidences from CTC time-steps. Used by the lite build; train/export your own from the
  library (see scripts/finetune_trocr.py and Library.export_dataset).
* TrocrEngine - optional TrOCR (Optimum/ONNX) for the full build; lazily imported.
Both are line recognisers; `segment_lines` cuts a zone into text lines first.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from ..models import Word
from .base import OcrEngine, split_line_into_words


def segment_lines(img: Image.Image, min_h: int = 12) -> list[tuple[int, int]]:
    """Split a crop into text lines (y ranges) using a smoothed horizontal ink profile."""
    g = np.asarray(img.convert("L"))
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    prof = bw.sum(axis=1).astype(np.float64)
    if prof.max() <= 0:
        return []
    k = max(3, g.shape[0] // 40) | 1
    prof = np.convolve(prof, np.ones(k) / k, mode="same")
    on = prof > prof.max() * 0.08
    lines, start = [], None
    for y, v in enumerate(on):
        if v and start is None:
            start = y
        elif not v and start is not None:
            if y - start >= min_h:
                lines.append((start, y))
            start = None
    if start is not None and len(on) - start >= min_h:
        lines.append((start, len(on)))
    if not lines:
        return [(0, g.shape[0])]
    pad = 4
    return [(max(0, a - pad), min(g.shape[0], b + pad)) for a, b in lines]


def ctc_greedy(logits: np.ndarray, charset: list[str], blank: int = 0):
    """Greedy CTC decode. logits: (T, C). Returns (text, mean_conf, [(char, t_start, t_end, conf)])."""
    ex = np.exp(logits - logits.max(axis=1, keepdims=True))
    probs = ex / ex.sum(axis=1, keepdims=True)
    idx = probs.argmax(axis=1)
    conf = probs.max(axis=1)
    out, prev, i, T = [], blank, 0, len(idx)
    while i < T:
        k = int(idx[i])
        if k == blank:
            prev = blank
            i += 1
            continue
        if k == prev:                       # same symbol repeated without a blank between
            i += 1
            continue
        j = i
        while j + 1 < T and idx[j + 1] == k:
            j += 1
        ci = k - 1 if blank == 0 else k
        if 0 <= ci < len(charset):
            out.append((charset[ci], i, j + 1, float(conf[i:j + 1].mean())))
        prev = k
        i = j + 1
    text = "".join(c[0] for c in out)
    mc = float(np.mean([c[3] for c in out])) if out else 0.0
    return text, mc, out


class CtcOnnxRecognizer(OcrEngine):
    name = "handwriting-ctc"

    def __init__(self, model_dir: str, height: int = 48, threads: int = 0, blank: int = 0):
        import onnxruntime as ort
        d = Path(model_dir)
        model = d / "rec.onnx"
        chars = d / "charset.txt"
        if not model.exists() or not chars.exists():
            raise FileNotFoundError(f"{d} must contain rec.onnx and charset.txt")
        so = ort.SessionOptions()
        if threads:
            so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        lines = chars.read_text(encoding="utf-8").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        self.charset = lines + [" "]
        self.blank = blank
        self.height = height

    @staticmethod
    def available(model_dir: str = "") -> bool:
        return bool(model_dir) and (Path(model_dir) / "rec.onnx").exists() and (Path(model_dir) / "charset.txt").exists()

    def _line(self, img: Image.Image):
        w, h = img.size
        nw = max(8, min(3200, int(round(self.height * w / max(h, 1)))))
        a = np.asarray(img.convert("RGB").resize((nw, self.height), Image.BILINEAR), dtype=np.float32)
        a = (a / 255.0 - 0.5) / 0.5
        a = a.transpose(2, 0, 1)[None]
        out = self.sess.run(None, {self.inp: a})[0]
        if out.ndim == 3:
            out = out[0] if out.shape[0] == 1 else out[:, 0, :]
        return out, nw

    def recognize(self, img: Image.Image, whitelist: str = "", fast: bool = False, psm: int = 0) -> list[Word]:
        words: list[Word] = []
        for (y0, y1) in segment_lines(img):
            crop = img.crop((0, y0, img.size[0], y1))
            # trim horizontal whitespace so CTC time-steps map to ink
            g = np.asarray(crop.convert("L"))
            cols = np.where((g < 160).any(axis=0))[0]
            x_off = int(cols[0]) if len(cols) else 0
            x_end = int(cols[-1]) + 1 if len(cols) else crop.size[0]
            crop = crop.crop((max(0, x_off - 4), 0, min(crop.size[0], x_end + 4), crop.size[1]))
            x_off = max(0, x_off - 4)
            logits, nw = self._line(crop)
            text, conf, chars = ctc_greedy(logits, self.charset, self.blank)
            if not text.strip():
                continue
            T = logits.shape[0]
            sx = crop.size[0] / float(T)
            ch = []
            for c, t0, t1, cf in chars:
                ch.append([c, [x_off + t0 * sx, y0, x_off + t1 * sx, y1], cf])
            box = [x_off, y0, x_off + crop.size[0], y1]
            words.extend(split_line_into_words(text, box, conf, ch))
        for w in words:
            w.source = self.name
            if whitelist:
                w.text = "".join(c for c in w.text if c in whitelist)
        return [w for w in words if w.text]


class TrocrEngine(OcrEngine):
    """Optional TrOCR via optimum.onnxruntime (full build, `pip install optimum[onnxruntime] transformers`)."""
    name = "trocr"

    def __init__(self, model_dir: str, threads: int = 0):
        from optimum.onnxruntime import ORTModelForVision2Seq
        from transformers import TrOCRProcessor
        self.proc = TrOCRProcessor.from_pretrained(model_dir)
        self.model = ORTModelForVision2Seq.from_pretrained(model_dir)

    @staticmethod
    def available(model_dir: str = "") -> bool:
        if not model_dir or not (Path(model_dir) / "config.json").exists():
            return False
        try:
            import optimum.onnxruntime  # noqa: F401
            import transformers  # noqa: F401
            return True
        except ImportError:
            return False

    def recognize(self, img: Image.Image, whitelist: str = "", fast: bool = False, psm: int = 0) -> list[Word]:
        words: list[Word] = []
        for (y0, y1) in segment_lines(img):
            crop = img.crop((0, y0, img.size[0], y1)).convert("RGB")
            pv = self.proc(images=crop, return_tensors="pt").pixel_values
            out = self.model.generate(pv, output_scores=True, return_dict_in_generate=True, max_new_tokens=64)
            text = self.proc.batch_decode(out.sequences, skip_special_tokens=True)[0].strip()
            if not text:
                continue
            try:
                import torch
                sc = self.model.compute_transition_scores(out.sequences, out.scores, normalize_logits=True)
                conf = float(torch.exp(sc).mean())
            except Exception:
                conf = 0.5
            box = [0, y0, img.size[0], y1]
            words.extend(split_line_into_words(text, box, conf))
        for w in words:
            w.source = self.name
        return words
