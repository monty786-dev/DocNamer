"""Geometry-preserving image clean-up for OCR (boxes stay valid)."""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from .settings import PreprocessCfg


def _gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"))


def cap_pixels(img: Image.Image, max_pixels: int) -> Image.Image:
    w, h = img.size
    if w * h <= max_pixels:
        return img
    s = (max_pixels / float(w * h)) ** 0.5
    return img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)


def analyse(gray: np.ndarray) -> dict:
    """Cheap quality metrics used by the 'auto' mode."""
    small = cv2.resize(gray, (0, 0), fx=min(1.0, 800 / max(gray.shape)), fy=min(1.0, 800 / max(gray.shape)))
    bg = cv2.medianBlur(cv2.dilate(small, np.ones((15, 15), np.uint8)), 21)
    illum_var = float(np.std(bg))
    contrast = float(np.percentile(small, 95) - np.percentile(small, 5))
    lap = float(cv2.Laplacian(small, cv2.CV_64F).var())
    return {"illum_var": illum_var, "contrast": contrast, "sharp": lap}


def remove_shadow(gray: np.ndarray) -> np.ndarray:
    dil = cv2.dilate(gray, np.ones((7, 7), np.uint8))
    bg = cv2.medianBlur(dil, 21)
    diff = 255 - cv2.absdiff(gray, bg)
    return cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX)


def remove_dark_border(gray: np.ndarray) -> np.ndarray:
    """Whiten dark scanner borders connected to the image edge (no cropping)."""
    h, w = gray.shape
    bw = (gray < 60).astype(np.uint8)
    mask = np.zeros((h + 2, w + 2), np.uint8)
    out = gray.copy()
    band = max(3, min(h, w) // 100)
    for (x, y) in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if bw[y, x]:
            tmp = bw.copy()
            cv2.floodFill(tmp, mask, (x, y), 2)
    filled = mask[1:-1, 1:-1] > 0
    # only accept when the filled area hugs the border (a frame), not the whole page
    if filled.any() and filled.mean() < 0.25:
        out[filled] = 255
    return out


def preprocess(img: Image.Image, cfg: PreprocessCfg) -> Image.Image:
    if cfg.mode == "off":
        return img
    g = _gray(img)
    shadow, denoise, contrast, binarize, border = cfg.shadow, cfg.denoise, cfg.contrast, cfg.binarize, cfg.border
    if cfg.mode == "auto":
        m = analyse(g)
        shadow = m["illum_var"] > 28
        contrast = m["contrast"] < 90
        denoise = m["sharp"] > 2500 and m["contrast"] < 140   # noisy, speckled scans
        binarize = False
        border = False
    if not any((shadow, denoise, contrast, binarize, border)):
        return img
    if border:
        g = remove_dark_border(g)
    if shadow:
        g = remove_shadow(g)
    if denoise:
        g = cv2.fastNlMeansDenoising(g, None, 10, 7, 21) if g.size < 6_000_000 else cv2.medianBlur(g, 3)
    if contrast:
        g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
    if binarize:
        g = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 15)
    return Image.fromarray(g).convert("RGB")


def ink_fraction(img: Image.Image) -> float:
    """Fraction of dark pixels - used for blank-page detection."""
    g = _gray(img)
    small = cv2.resize(g, (0, 0), fx=min(1.0, 600 / max(g.shape)), fy=min(1.0, 600 / max(g.shape)))
    # remove thin border noise
    hh, ww = small.shape
    m = max(2, int(min(hh, ww) * 0.02))
    small = small[m:hh - m, m:ww - m]
    thr = max(60, min(200, int(np.percentile(small, 50)) - 40))
    return float((small < thr).mean())


def is_blank_page(img: Image.Image, threshold: float = 0.003) -> bool:
    """Blank = almost no ink AND no text-sized blob. A page with only a short line of text
    (ink well below `threshold`) is NOT blank; scanner specks / grey noise are."""
    if ink_fraction(img) >= threshold:
        return False
    g = _gray(img)
    k = min(1.0, 800 / max(g.shape))
    small = cv2.resize(g, (0, 0), fx=k, fy=k, interpolation=cv2.INTER_AREA)
    hh, ww = small.shape
    m = max(2, int(min(hh, ww) * 0.02))
    small = small[m:hh - m, m:ww - m]
    thr = max(60, min(200, int(np.percentile(small, 50)) - 40))
    bw = (small < thr).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    big = sum(1 for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= 12 and stats[i, cv2.CC_STAT_HEIGHT] >= 5)
    return big == 0
