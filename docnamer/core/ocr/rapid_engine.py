"""RapidOCR (PP-OCR models on onnxruntime). Default printed-text engine; CPU only.

RapidOCR returns text LINES; we split them into words and approximate character boxes by even
spacing (documented limitation). For true per-character boxes use the CTC handwriting recogniser.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from ..models import Word
from .base import OcrEngine, split_line_into_words


class RapidOcrEngine(OcrEngine):
    name = "rapidocr"

    def __init__(self, threads: int = 0, model_dir: str = "", char_level: bool = True):
        from rapidocr_onnxruntime import RapidOCR   # lazy: not needed for Tesseract-only runs
        kw = {}
        if threads:
            kw.update(intra_op_num_threads=threads, inter_op_num_threads=1)
        if model_dir:
            import os
            for key, fn in (("det_model_path", "det.onnx"), ("rec_model_path", "rec.onnx"),
                            ("cls_model_path", "cls.onnx"), ("rec_keys_path", "rec_keys.txt")):
                p = os.path.join(model_dir, fn)
                if os.path.exists(p):
                    kw[key] = p
        try:
            self.ocr = RapidOCR(**kw)
        except TypeError:          # older/newer signature: fall back to bundled defaults
            self.ocr = RapidOCR()
        self.char_level = char_level

    @staticmethod
    def available() -> bool:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            return True
        except ImportError:
            return False

    def recognize(self, img: Image.Image, whitelist: str = "", fast: bool = False, psm: int = 0) -> list[Word]:
        arr = np.asarray(img.convert("RGB"))[:, :, ::-1]       # RGB -> BGR
        result, _ = self.ocr(arr)
        words: list[Word] = []
        for item in result or []:
            poly, text, score = item[0], item[1], item[2]
            text = (text or "").strip()
            if not text:
                continue
            try:
                conf = float(score)
            except (TypeError, ValueError):
                conf = 0.0
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            box = [min(xs), min(ys), max(xs), max(ys)]
            words.extend(split_line_into_words(text, box, conf))
        for w in words:
            w.source = self.name
            if not self.char_level:
                w.chars = None
        return words
