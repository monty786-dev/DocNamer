"""Page loading (render + orientation) and field extraction: zones, anchors, full read."""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image

from .layout import (Match, box_union, find_matches, group_lines, line_text, lines_to_text,
                     sub_box, words_in_rect, words_to_text)
from .models import FieldValue, Word
from .naming import parse_date
from .textutil import is_letters, to_ascii_digits
from .ocr import Engines, mean_conf
from .orientation import OrientResult, apply_transform, detect_orientation
from .preprocess import ink_fraction, is_blank_page, preprocess
from .readers import BaseDocument
from .settings import Anchor, FieldSpec, Settings, Zone

log = logging.getLogger("docnamer.extract")


# =====================================================================================
@dataclass
class PageCtx:
    doc: BaseDocument
    index: int
    image: Optional[Image.Image] = None          # upright working image (None for docx/xlsx)
    words: list = field(default_factory=list)    # native words/units in working coordinates
    has_native: bool = False
    scale: float = 1.0
    transform: dict = field(default_factory=lambda: {"pdf_rot": 0, "rot": 0, "skew": 0.0})
    orient: Optional[OrientResult] = None
    label: str = ""
    crop_dir: Optional[Path] = None
    tag: str = ""
    keep_crops: bool = False
    _ocr_image: Optional[Image.Image] = None
    _full: Optional[list] = None
    _prep_cfg: object = None

    @property
    def width(self) -> float:
        return float(self.image.size[0]) if self.image else 1.0

    @property
    def height(self) -> float:
        return float(self.image.size[1]) if self.image else 1.0

    @property
    def ocr_image(self) -> Image.Image:
        if self._ocr_image is None:
            self._ocr_image = preprocess(self.image, self._prep_cfg) if self._prep_cfg else self.image
        return self._ocr_image


def load_page(doc: BaseDocument, i: int, st: Settings, engines: Engines, forced: Optional[dict] = None) -> PageCtx:
    """Render page i, decide native-text vs OCR, and apply orientation/deskew (per page)."""
    ctx = PageCtx(doc, i, label=doc.page_label(i))
    ctx._prep_cfg = st.preprocess
    forced = (forced or {}).get(i, {})
    ocfg, cfg = st.orientation, st.ocr
    if not doc.visual:
        ctx.words = doc.native_words(i, 1.0)
        ctx.has_native = bool(ctx.words)
        return ctx

    native_len = doc.native_text_len(i) if doc.kind == "pdf" else 0
    use_native = doc.kind == "pdf" and native_len >= 5 and not cfg.force_ocr
    if use_native:
        pdf_rot = forced.get("pdf_rot")
        if pdf_rot is None:
            pdf_rot = doc.native_orientation(i) if ocfg.mode != "off" else 0
        img, scale = doc.render(i, cfg.dpi, cfg.max_pixels, rot=pdf_rot)
        ctx.image, ctx.scale = img, scale
        ctx.words = doc.native_words(i, scale, rot=pdf_rot)
        ctx.has_native = bool(ctx.words)
        ctx.transform.update(pdf_rot=pdf_rot)
        if ocfg.scan_native_pages and ocfg.mode != "off":
            _orient(ctx, st, engines, forced, rebuild_native=False)
        return ctx

    img, scale = doc.render(i, cfg.dpi, cfg.max_pixels)
    ctx.image, ctx.scale = img, scale
    _orient(ctx, st, engines, forced)
    return ctx


def _orient(ctx: PageCtx, st: Settings, engines: Engines, forced: dict, rebuild_native: bool = True) -> None:
    ocfg = st.orientation
    force_cw = forced.get("rot")
    if ocfg.mode == "off" and force_cw is None:
        return
    res = detect_orientation(ctx.image, ocfg, engines, st.ocr.tesseract_cmd, force_cw)
    if force_cw is not None and "skew" in forced:
        res.skew = forced["skew"]
    ctx.orient = res
    if res.cw or abs(res.skew) > 0:
        ctx.image = apply_transform(ctx.image, res.cw, res.skew)
        ctx.transform.update(rot=res.cw, skew=res.skew)


def render_with_transform(doc: BaseDocument, i: int, st: Settings, tf: dict) -> Image.Image:
    """Deterministically re-create the working image of a page (used for marked images)."""
    img, _ = doc.render(i, st.ocr.dpi, st.ocr.max_pixels, rot=tf.get("pdf_rot", 0))
    return apply_transform(img, tf.get("rot", 0), tf.get("skew", 0.0))


def page_words(ctx: PageCtx, st: Settings, engines: Engines) -> list[Word]:
    """Native words when available, otherwise a (cached) full-page OCR."""
    if ctx.has_native:
        return ctx.words
    if ctx._full is None:
        ctx._full = []
        if ctx.image is not None and engines.available():
            ws, _ = engines.recognize(ctx.ocr_image, kind="printed")
            ctx._full = ws
    return ctx._full


def page_text(ctx: PageCtx, st: Settings, engines: Engines) -> str:
    return words_to_text(page_words(ctx, st, engines))


def is_blank(ctx: PageCtx, st: Settings) -> bool:
    if ctx.has_native:
        return sum(len(w.text) for w in ctx.words) < 3
    if ctx.image is None:
        return True
    return is_blank_page(ctx.image, st.rules.blank_threshold)


def has_qr(ctx: PageCtx) -> bool:
    try:
        import cv2
        import numpy as np
        if ctx.image is None:
            return False
        a = np.asarray(ctx.image.convert("RGB"))[:, :, ::-1]
        data, pts, _ = cv2.QRCodeDetector().detectAndDecode(a)
        return bool(data)
    except Exception:
        return False


# =====================================================================================
def pages_in(spec: str, page_index: int) -> bool:
    """Is 0-based page_index inside a 1-based '1,3-5' spec ('' = always)?"""
    spec = (spec or "").strip()
    if not spec:
        return True
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                if int(a or 1) <= page_index + 1 <= int(b or 10**6):
                    return True
            except ValueError:
                continue
        elif part.isdigit() and int(part) == page_index + 1:
            return True
    return False


def validate(text: str, spec: FieldSpec) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if spec.fmt == "date":
        if parse_date(t) is None and not re.search(r"\d{1,4}[-/. ]\d{1,2}[-/. ]\d{1,4}", to_ascii_digits(t)):
            return False
    elif spec.fmt == "number":
        if not re.fullmatch(r"[+-]?[\d.,\s]+", to_ascii_digits(t)):
            return False
    elif spec.fmt == "letters":
        if not is_letters(t):          # any script incl. Hindi (matras are combining marks, not \w)
            return False
    if spec.regex:
        try:
            if not re.search(spec.regex, t):
                return False
        except re.error:
            pass
    return True


def _clean(text: str, spec: FieldSpec) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    if spec.whitelist:
        t = "".join(c for c in t if c in spec.whitelist or c == " ").strip()
    return t


def _finish(fv: FieldValue, spec: FieldSpec, st: Settings, ctx: PageCtx, rect=None) -> FieldValue:
    fv.raw = fv.text
    fv.required = spec.required
    fv.text = _clean(fv.text, spec)
    fv.found = bool(fv.text)
    fv.page = ctx.index
    fv.valid = validate(fv.text, spec)
    floor = max(st.ocr.min_conf, st.rules.min_conf)
    fv.needs_review = bool(fv.found and (fv.conf < floor or not fv.valid)) or (spec.required and not fv.found and fv.source != "n/a")
    keep = ctx.keep_crops and spec.kind == "handwritten" and fv.found
    if (fv.needs_review or keep) and ctx.image is not None and ctx.crop_dir is not None:
        box = fv.box or (list(rect) if rect else None)
        if box:
            try:
                p = 8
                crop = ctx.image.crop((max(0, box[0] - p), max(0, box[1] - p), min(ctx.width, box[2] + p), min(ctx.height, box[3] + p)))
                ctx.crop_dir.mkdir(parents=True, exist_ok=True)
                path = ctx.crop_dir / f"{ctx.tag}_{re.sub(r'[^A-Za-z0-9_-]', '_', spec.name)}_p{ctx.index}.png"
                crop.save(path)
                fv.crop_path = str(path)
            except Exception as e:
                log.debug("crop save failed: %s", e)
    return fv


# =====================================================================================
def read_rect(ctx: PageCtx, rect: tuple, spec: FieldSpec, st: Settings, engines: Engines) -> FieldValue:
    """Read text inside a pixel rectangle: native text if any, else OCR (printed / handwriting)."""
    fv = FieldValue(name=spec.name)
    x0, y0, x1, y1 = rect
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(ctx.width, x1), min(ctx.height, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return fv
    rect = (x0, y0, x1, y1)
    if ctx.has_native and spec.kind != "handwritten":
        ws = words_in_rect(ctx.words, rect)
        if ws:
            lines = group_lines(ws)
            fv.text = lines_to_text(lines, " ")
            fv.conf, fv.source = 1.0, "native"
            fv.boxes = [w.box for w in ws]
            fv.box = box_union(fv.boxes)
            fv.chars = [c for w in ws for c in (w.chars or [])]
            return fv
    if ctx.image is None:
        return fv
    if not engines.available():
        fv.note = "no OCR engine available"
        return fv
    pad = 6
    cx0, cy0 = max(0, int(x0) - pad), max(0, int(y0) - pad)
    cx1, cy1 = min(int(ctx.width), int(x1) + pad), min(int(ctx.height), int(y1) + pad)
    crop = ctx.ocr_image.crop((cx0, cy0, cx1, cy1))
    words, cands = engines.recognize(crop, kind=spec.kind, whitelist=spec.whitelist, min_conf=st.ocr.min_conf)
    for w in words:
        w.box = [w.box[0] + cx0, w.box[1] + cy0, w.box[2] + cx0, w.box[3] + cy0]
        if w.chars:
            for c in w.chars:
                c[1] = [c[1][0] + cx0, c[1][1] + cy0, c[1][2] + cx0, c[1][3] + cy0]
    fv.candidates = cands
    if words:
        fv.text = words_to_text(words, " ")
        fv.conf = mean_conf(words)
        fv.source = words[0].source
        fv.boxes = [w.box for w in words]
        fv.box = box_union(fv.boxes)
        fv.chars = [c for w in words for c in (w.chars or [])]
    else:
        fv.box = list(rect)
        fv.source = "ocr"
    return fv


# ---- zones -----------------------------------------------------------------------------
def _cell_match(w: Word, addr: str) -> bool:
    addr = addr.strip().upper()
    ref = w.ref.upper()
    if "!" in addr:
        return ref == addr
    return ref.endswith("!" + addr)


def extract_zone(ctx: PageCtx, z: Zone, st: Settings, engines: Engines) -> FieldValue:
    if not pages_in(z.pages, ctx.index):
        return FieldValue(name=z.name, source="n/a")
    if z.cell:                                      # spreadsheet cell zone
        fv = FieldValue(name=z.name)
        for w in ctx.words:
            if _cell_match(w, z.cell):
                fv.text, fv.conf, fv.source, fv.ref = w.text, 1.0, "native", w.ref
                break
        return _finish(fv, z, st, ctx)
    if not ctx.doc.visual:
        return FieldValue(name=z.name, source="n/a")
    W, H = ctx.width, ctx.height
    rect = (z.x * W, z.y * H, (z.x + z.w) * W, (z.y + z.h) * H)
    return _finish(read_rect(ctx, rect, z, st, engines), z, st, ctx, rect)


# ---- anchors ---------------------------------------------------------------------------
def _grid(words: list[Word]) -> dict:
    g = {}
    for w in words:
        if w.row >= 0:
            g[(w.ref.split("!")[0].split(":")[0], w.row, w.col)] = w
    return g


def _unit_neighbor(words: list[Word], w: Word, direction: str) -> Optional[Word]:
    if w.row >= 0:
        key = (w.ref.split("!")[0].split(":")[0], w.row, w.col)
        g = _grid(words)
        if direction == "below":
            return g.get((key[0], key[1] + 1, key[2]))
        for dc in range(1, 6):                       # first non-empty cell to the right
            n = g.get((key[0], key[1], key[2] + dc))
            if n:
                return n
        return None
    idx = words.index(w)
    return words[idx + 1] if idx + 1 < len(words) else None


def extract_anchor(ctx: PageCtx, a: Anchor, st: Settings, engines: Engines) -> FieldValue:
    fv = FieldValue(name=a.name)
    if not pages_in(a.pages, ctx.index):
        fv.source = "n/a"
        return fv
    words = page_words(ctx, st, engines)
    if not words:
        return _finish(fv, a, st, ctx)
    lines = group_lines(words)
    ms = find_matches(lines, a.keyword, a.is_regex, a.case_sensitive)
    if len(ms) < a.occurrence:
        return _finish(fv, a, st, ctx)
    m: Match = ms[a.occurrence - 1]
    line = lines[m.line]
    strip = a.strip_chars

    # ---- unit pages (docx / xlsx / csv) -----------------------------------------
    if line[m.first].box is None:
        w = line[m.last]
        tail = m.tail.strip(strip)
        if tail:
            fv.text, fv.ref = tail, w.ref
        else:
            n = _unit_neighbor(words, w, "below" if a.direction == "below" else "right")
            if n:
                fv.text, fv.ref = n.text, n.ref
        fv.conf, fv.source = 1.0, "native"
        return _finish(fv, a, st, ctx)

    # ---- boxed pages --------------------------------------------------------------------
    first, last = line[m.first], line[m.last]
    abox = box_union([first.box, last.box])
    ah = max(abox[3] - abox[1], 1.0)
    W, H = ctx.width, ctx.height

    if a.direction == "offset":
        dx, dy, dw, dh = a.offset
        rect = (abox[0] + dx * W, abox[1] + dy * H, abox[0] + (dx + dw) * W, abox[1] + (dy + dh) * H)
        return _finish(read_rect(ctx, rect, a, st, engines), a, st, ctx, rect)

    if a.direction == "below":
        x_lo, x_hi = abox[0] - ah, abox[0] + a.max_width * W
        cand = [w for i, l in enumerate(lines) if i != m.line for w in l
                if w.box and abox[3] - 0.3 * ah <= w.cy <= abox[3] + a.max_height * ah and x_lo <= w.cx <= x_hi]
        if a.kind == "handwritten" or not cand:
            rect = (x_lo, abox[3], x_hi, abox[3] + a.max_height * ah)
            if a.kind == "handwritten":
                return _finish(read_rect(ctx, rect, a, st, engines), a, st, ctx, rect)
        got = group_lines(cand)[: max(1, a.lines_below)]
        ws = [w for l in got for w in l]
        fv.text = lines_to_text(got, " ")
        fv.boxes = [w.box for w in ws]
        fv.box = box_union(fv.boxes)
        fv.conf = mean_conf(ws) if ws else 0.0
        fv.source = ws[0].source if ws else ""
        fv.chars = [c for w in ws for c in (w.chars or [])]
        return _finish(fv, a, st, ctx, abox)

    # direction == right
    if a.kind == "handwritten":
        rect = (abox[2], abox[1] - 0.3 * ah, abox[2] + a.max_width * W, abox[3] + 0.3 * ah)
        return _finish(read_rect(ctx, rect, a, st, engines), a, st, ctx, rect)
    rest = [w for w in line[m.last + 1:] if w.box and w.box[0] <= abox[2] + a.max_width * W]
    parts, boxes = [], []
    if m.tail.strip(strip):                       # skip punctuation-only tails such as ":"
        frac = 1.0 - len(m.tail) / max(len(last.text), 1)
        parts.append(m.tail)
        boxes.append(sub_box(last, frac))
    parts += [w.text for w in rest]
    boxes += [w.box for w in rest]
    text = " ".join(parts).lstrip(strip).strip()
    fv.text = text
    fv.boxes = boxes
    fv.box = box_union(boxes)
    sel = rest if rest else [last]
    fv.conf = mean_conf(sel)
    fv.source = last.source
    fv.chars = [c for w in rest for c in (w.chars or [])]
    return _finish(fv, a, st, ctx, abox)


# ---- full read -----------------------------------------------------------------------
def read_full(ctx: PageCtx, st: Settings, engines: Engines) -> tuple[str, list[Word]]:
    ws = page_words(ctx, st, engines)
    return words_to_text(ws), ws
