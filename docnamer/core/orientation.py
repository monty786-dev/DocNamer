"""Auto orientation: 0/90/180/270 detection (Tesseract OSD or OCR voting) + fine deskew.

Convention: `cw` is the number of degrees CLOCKWISE the page must be turned to be upright.
`skew` is the CCW rotation (degrees, PIL/cv2 convention) that straightens a tilted page.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from .env import find_tesseract
from .settings import OrientationCfg

log = logging.getLogger("docnamer.orientation")


@dataclass
class OrientResult:
    cw: int = 0
    conf: float = 1.0
    skew: float = 0.0
    method: str = "none"
    uncertain: bool = False
    guess: int = 0


def rotate_cw(img: Image.Image, cw: int) -> Image.Image:
    cw %= 360
    if cw == 0:
        return img
    return img.transpose({90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}[cw])


def rotate_skew(img: Image.Image, deg: float) -> Image.Image:
    if abs(deg) < 1e-3:
        return img
    return img.rotate(deg, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))


def apply_transform(img: Image.Image, cw: int, skew: float) -> Image.Image:
    return rotate_skew(rotate_cw(img, cw), skew)


# ----------------------------------------------------------------------------------
def _binary_ink(gray: np.ndarray, max_side: int = 1000) -> np.ndarray:
    s = min(1.0, max_side / max(gray.shape))
    small = cv2.resize(gray, (0, 0), fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else gray
    small = cv2.GaussianBlur(small, (3, 3), 0)
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def text_axis_score(gray: np.ndarray) -> float:
    """>1 means text runs horizontally, <1 means vertically (page rotated 90/270)."""
    bw = _binary_ink(gray, 800)
    if bw.mean() < 1:
        return 1.0
    rows = bw.sum(axis=1).astype(np.float64)
    cols = bw.sum(axis=0).astype(np.float64)
    return float((np.var(rows) + 1) / (np.var(cols) + 1))


def estimate_skew(img: Image.Image, max_deg: float = 12.0) -> tuple[float, float]:
    """Return (ccw_degrees_to_apply, confidence 0..1) using a projection-profile search."""
    gray = np.asarray(img.convert("L"))
    bw = _binary_ink(gray, 900)
    if bw.mean() < 0.5:                       # blank-ish page
        return 0.0, 0.0
    h, w = bw.shape
    centre = (w / 2, h / 2)

    def score(a: float) -> float:
        M = cv2.getRotationMatrix2D(centre, a, 1.0)
        r = cv2.warpAffine(bw, M, (w, h), flags=cv2.INTER_NEAREST)
        p = r.sum(axis=1).astype(np.float64)
        return float(np.sum(np.diff(p) ** 2))

    base = score(0.0)
    coarse = np.arange(-max_deg, max_deg + 1e-6, 1.0)
    best = max(coarse, key=score)
    fine = np.arange(best - 1.0, best + 1.0 + 1e-6, 0.1)
    best = max(fine, key=score)
    gain = score(best) / (base + 1.0)
    conf = float(min(1.0, max(0.0, (gain - 1.0) / 0.5)))
    return float(best), conf


# ----------------------------------------------------------------------------------
def osd_rotation(img: Image.Image, tesseract_cmd: str = "") -> Optional[tuple[int, float]]:
    cmd = find_tesseract(tesseract_cmd)
    if not cmd:
        return None
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = cmd
        small = img.copy()
        small.thumbnail((1800, 1800))
        d = pytesseract.image_to_osd(small, output_type=pytesseract.Output.DICT,
                                     config="--psm 0 -c min_characters_to_try=10")
        rot = int(d.get("rotate", 0)) % 360
        conf = min(1.0, float(d.get("orientation_conf", 0.0)) / 3.0)   # ~2.5+ raw = reliable
        return rot, conf
    except Exception as e:      # OSD fails on pages with too little text
        log.debug("OSD failed: %s", e)
        return None


def vote_rotation(img: Image.Image, engines) -> Optional[tuple[int, float]]:
    """Fallback: OCR the page at candidate angles and keep the one that reads best."""
    if engines is None or not engines.available():
        return None
    small = img.copy()
    small.thumbnail((1100, 1100))
    gray = np.asarray(small.convert("L"))
    horizontal = text_axis_score(gray) >= 1.0
    cands = [0, 180] if horizontal else [90, 270]
    scores = {}
    for c in cands:
        words, _ = engines.recognize(rotate_cw(small, c), kind="printed", fast=True)
        scores[c] = sum(len("".join(ch for ch in w.text if ch.isalnum())) * w.conf for w in words)
    best = max(cands, key=lambda c: scores[c] - (0.02 * scores[c] if c in (180, 270) else 0))
    other = [c for c in cands if c != best][0]
    if scores[best] < 15:
        return best, 0.0
    conf = (scores[best] - scores[other]) / max(scores[best], 1.0)
    return best, float(min(1.0, conf * 1.5))


def detect_orientation(img: Image.Image, cfg: OrientationCfg, engines=None,
                       tesseract_cmd: str = "", force_cw: Optional[int] = None) -> OrientResult:
    """Full orientation analysis of one page image."""
    if cfg.mode == "off" and force_cw is None:
        return OrientResult(method="off")
    res = OrientResult()
    if force_cw is not None:
        res.cw, res.conf, res.method = force_cw % 360, 1.0, "forced"
    else:
        gray = np.asarray(img.convert("L"))
        axis = text_axis_score(gray)
        r = osd_rotation(img, tesseract_cmd)
        if r is not None and r[1] >= 0.25:
            res.cw, res.conf, res.method = r[0], r[1], "osd"
        else:
            v = vote_rotation(img, engines)
            if v is not None:
                res.cw, res.conf, res.method = v[0], v[1], "ocr-vote"
            elif axis < 0.5:       # no OCR available: at least fix sideways text
                res.cw, res.conf, res.method = 90, 0.3, "axis"
            else:
                res.method, res.conf = "none", 0.0
        if res.cw != 0 and res.conf < cfg.threshold:
            res.uncertain = True
            res.guess = res.cw
            if cfg.mode == "ask":
                res.cw = 0          # caller decides; keep upright guess untouched until answered
    if cfg.deskew:
        upright = rotate_cw(img, res.cw)
        a, c = estimate_skew(upright, cfg.max_skew)
        if abs(a) >= cfg.min_skew and c >= 0.3:
            res.skew = round(a, 2)
    return res
