"""OCR engine interface. Every engine returns list[Word] in the coordinates of the image it was given."""
from __future__ import annotations

from abc import ABC, abstractmethod

from PIL import Image

from ..models import Word


class OcrEngine(ABC):
    name = "base"

    @abstractmethod
    def recognize(self, img: Image.Image, whitelist: str = "", fast: bool = False, psm: int = 0) -> list[Word]:
        """Return words (with char boxes/confidences where the model supports it)."""

    @staticmethod
    def available() -> bool:
        return True


def spread_chars(text: str, box: list, conf: float) -> list:
    """Approximate per-character boxes inside a text unit by equal spacing (used when a
    model gives only line/word boxes)."""
    if not text:
        return []
    x0, y0, x1, y1 = box
    n = len(text)
    wch = (x1 - x0) / n
    return [[c, [x0 + i * wch, y0, x0 + (i + 1) * wch, y1], conf] for i, c in enumerate(text)]


def split_line_into_words(text: str, box: list, conf: float, chars: list | None = None) -> list[Word]:
    """Split an OCR text line into words with proportional boxes."""
    x0, y0, x1, y1 = box
    ch = chars or spread_chars(text, box, conf)
    out: list[Word] = []
    cur: list = []
    for c in ch:
        if c[0].isspace():
            if cur:
                out.append(cur)
                cur = []
        else:
            cur.append(c)
    if cur:
        out.append(cur)
    words = []
    for grp in out:
        txt = "".join(c[0] for c in grp)
        bx = [min(c[1][0] for c in grp), y0, max(c[1][2] for c in grp), y1]
        cf = sum(c[2] for c in grp) / len(grp)
        words.append(Word(text=txt, box=bx, conf=cf, chars=grp))
    return words
