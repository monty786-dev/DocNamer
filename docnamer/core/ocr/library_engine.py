"""Handwriting recognition from the user's own taught samples (no model download needed).

Pipeline: line -> words (shabd) -> match each word against taught WORD samples; if no confident
match, split into letters (akshar) and match each against taught LETTER samples.
Confidence is calibrated from HOG similarity and is deliberately conservative: unknown or
ambiguous writing gets a low score so the app asks you (review queue) instead of guessing."""
from __future__ import annotations

import numpy as np
from PIL import Image

from ..library import Library
from ..models import Word
from ..segment import deskew, segment_letters, segment_lines, segment_words, tight_y
from ..textutil import graphemes
from .base import OcrEngine

PRE_BASE_MATRA = "\u093f"       # ि is written LEFT of its consonant but typed AFTER it


def calibrate(sim: float) -> float:
    """HOG cosine similarity -> confidence 0..1 (same-writer words ~0.9+, unrelated ~0.5-0.8)."""
    return float(min(1.0, max(0.0, (sim - 0.78) / 0.17)))


def compose_letters(labels: list[str]) -> str:
    """Join letter labels in LOGICAL order (swap a left-side 'ि' with the following consonant)."""
    out = list(labels)
    i = 0
    while i < len(out) - 1:
        if out[i] == PRE_BASE_MATRA and out[i + 1] and out[i + 1][0] not in "\u093e\u0940\u0941\u0942\u0947\u0948\u094b\u094c\u0902\u0903":
            out[i], out[i + 1] = out[i + 1], out[i]
            i += 1
        i += 1
    return "".join(out)


class LibraryEngine(OcrEngine):
    name = "library"

    def __init__(self, lib: Library, word_floor: float = 0.78, char_floor: float = 0.72):
        self.lib, self.word_floor, self.char_floor = lib, word_floor, char_floor

    @staticmethod
    def available(lib: Library | None = None) -> bool:
        return bool(lib and lib.count() > 0)

    def read_word(self, wimg: Image.Image):
        """-> (text, confidence, chars[[g, [x0,y0,x1,y1], conf]]) for ONE word crop."""
        cands = self.lib.suggest(wimg, k=3, min_sim=self.word_floor, unit="word")
        if cands:
            best = cands[0]
            runner = next((c for c in cands[1:] if c[0] != best[0]), None)
            conf = calibrate(best[1])
            if runner and best[1] - runner[1] < 0.04:       # ambiguous between two taught words
                conf *= 0.5
            return best[0], conf, None
        if self.lib.count("char") == 0:
            return "", 0.0, None
        H = wimg.size[1]
        labels, confs, chars = [], [], []
        for (a, b) in segment_letters(wimg):
            piece = wimg.crop((a, 0, b, H))
            m = self.lib.suggest(piece, k=2, min_sim=self.char_floor, unit="char")
            if m:
                ch, c = m[0][0], calibrate(m[0][1])
                if len(m) > 1 and m[0][1] - m[1][1] < 0.03:
                    c *= 0.6
            else:
                ch, c = "?", 0.0
            labels.append(ch)
            confs.append(c)
            chars.append([ch, [a, 0, b, H], c])
        if not labels or all(l == "?" for l in labels):      # nothing recognised: say so, don't invent text
            return "", 0.0, None
        text = compose_letters(labels)
        conf = float(np.mean(confs)) * 0.85
        if "?" in labels:
            conf = min(conf, 0.3)
        return text, conf, chars

    def recognize(self, img: Image.Image, whitelist: str = "", fast: bool = False, psm: int = 0) -> list[Word]:
        words: list[Word] = []
        img = deskew(img)
        for (y0, y1) in segment_lines(img):
            line = img.crop((0, y0, img.size[0], y1))
            for (x0, x1) in segment_words(line):
                wimg = line.crop((x0, 0, x1, line.size[1]))
                text, conf, chars = self.read_word(wimg)
                if not text:
                    continue
                ty0, ty1 = tight_y(line, x0, x1)
                if whitelist:
                    text = "".join(c for c in text if c in whitelist)
                    if not text:
                        continue
                if chars:
                    chars = [[c, [x0 + b[0], y0 + b[1], x0 + b[2], y0 + b[3]], cf] for c, b, cf in chars]
                words.append(Word(text=text, box=[x0, y0 + ty0, x1, y0 + ty1], conf=conf, source=self.name, chars=chars))
        return words
