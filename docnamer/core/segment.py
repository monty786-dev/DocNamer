"""Segmentation of handwriting crops: lines -> words (shabd) -> letters (akshar).

Devanagari words are tied together by a headline (shirorekha). For letters we remove that band
first, then split on empty columns, so upper vowel signs stay with their consonant.
Latin: plain empty-column splitting (works for printed / block / spaced handwriting; joined
cursive cannot be split reliably - teach whole words instead)."""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image


def deskew(img: Image.Image, max_deg: float = 8.0) -> Image.Image:
    """Straighten a (slightly tilted) handwriting crop before segmenting. Same canvas size, so boxes
    stay within a few pixels of the original coordinates."""
    from .orientation import estimate_skew
    try:
        a, conf = estimate_skew(img, max_deg)
    except Exception:
        return img
    if abs(a) < 0.4 or conf < 0.15:
        return img
    return img.convert("RGB").rotate(a, resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))


def ink_mask(img: Image.Image) -> np.ndarray:
    g = np.asarray(img.convert("L"))
    if g.size == 0:
        return np.zeros((1, 1), bool)
    _, bw = cv2.threshold(cv2.GaussianBlur(g, (3, 3), 0), 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # a nearly blank crop makes Otsu split noise: require real contrast
    if g.max() - g.min() < 40:
        return np.zeros_like(bw, bool)
    return bw > 0


def _runs(flags: np.ndarray, merge_gap: int = 1, min_len: int = 1) -> list[tuple[int, int]]:
    out, start = [], None
    for i, v in enumerate(flags):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(flags)))
    merged: list[list[int]] = []
    for a, b in out:
        if merged and a - merged[-1][1] < merge_gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_len]


def segment_lines(img: Image.Image, min_h: int = 12) -> list[tuple[int, int]]:
    ink = ink_mask(img)
    prof = ink.sum(axis=1).astype(np.float64)
    if prof.max() <= 0:
        return []
    k = max(3, ink.shape[0] // 40) | 1
    prof = np.convolve(prof, np.ones(k) / k, mode="same")
    runs = _runs(prof > prof.max() * 0.08, 1, min_h)
    if not runs:
        return [(0, ink.shape[0])]
    pad = 4
    return [(max(0, a - pad), min(ink.shape[0], b + pad)) for a, b in runs]


def tight_y(img: Image.Image, x0: int = 0, x1: int | None = None) -> tuple[int, int]:
    ink = ink_mask(img)[:, x0:x1]
    rows = np.where(ink.any(axis=1))[0]
    return (int(rows[0]), int(rows[-1]) + 1) if len(rows) else (0, ink.shape[0])


def line_is_devanagari(ink: np.ndarray) -> bool:
    """A Devanagari line has a headline touching (nearly) every inked column; Latin lines do not."""
    cols = int(ink.any(axis=0).sum())
    return cols > 8 and float(ink.sum(axis=1).max()) / cols > 0.92


def segment_words(line: Image.Image, gap_ratio: float | None = None) -> list[tuple[int, int]]:
    """x-ranges of words in ONE text line.
    Devanagari: letters inside a word are joined by the headline, so any real gap (>= ~0.15 x ink height)
    is a word boundary. Latin: split on gaps >= ~0.30 x ink height (letter gaps are smaller)."""
    ink = ink_mask(line)
    rows = np.where(ink.any(axis=1))[0]
    if len(rows) == 0:
        return []
    ink_h = rows[-1] - rows[0] + 1
    ratio = gap_ratio if gap_ratio is not None else (0.15 if line_is_devanagari(ink) else 0.30)
    return _runs(ink.any(axis=0), max(4, int(ratio * ink_h)), 3)


def has_shirorekha(ink: np.ndarray, cover: float = 0.55) -> tuple[bool, int, int]:
    """Is there a long horizontal stroke (headline) in the upper half? Returns (found, y0, y1)."""
    h, w = ink.shape
    if w < 8 or h < 8:
        return False, 0, 0
    best, by = 0, 0
    for y in range(0, int(h * 0.6)):
        row = ink[y]
        if row.sum() == 0:
            continue
        longest = max((b - a for a, b in _runs(row, 3, 1)), default=0)
        if longest > best:
            best, by = longest, y
    if best < cover * w:
        return False, 0, 0
    y0 = by
    while y0 > 0 and _longest(ink[y0 - 1]) >= 0.5 * best:
        y0 -= 1
    y1 = by + 1
    while y1 < h and _longest(ink[y1]) >= 0.5 * best:
        y1 += 1
    return True, y0, y1


def _longest(row: np.ndarray) -> int:
    return max((b - a for a, b in _runs(row, 3, 1)), default=0)


def segment_letters(word: Image.Image, script: str = "auto") -> list[tuple[int, int]]:
    """x-ranges of letters (akshar) inside ONE word crop."""
    ink = ink_mask(word)
    if not ink.any():
        return []
    dev = False
    if script in ("auto", "devanagari"):
        dev, y0, y1 = has_shirorekha(ink)
        if script == "devanagari" and not dev:
            dev = False
    if dev:
        ink = ink.copy()
        ink[y0:y1, :] = False
    cols = ink.any(axis=0)
    h = ink.shape[0]
    return _runs(cols, 1, max(2, h // 12))
