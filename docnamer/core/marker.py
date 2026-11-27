"""Draw marks (rectangle / ellipse / underline / highlight) where the name data was read."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .models import Word
from .settings import MarkCfg

log = logging.getLogger("docnamer.marker")

_FONT_CANDIDATES = ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def _font(size: int):
    for n in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def hex_to_rgb(c: str) -> tuple:
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (229, 57, 53)


def draw_marks(img: Image.Image, marks: list[dict], cfg: MarkCfg) -> Image.Image:
    """marks: [{"boxes": [[x0,y0,x1,y1],...], "label": "invoice", "chars": [[c, box, conf],...]}]"""
    base = img.convert("RGBA")
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    rgb = hex_to_rgb(cfg.color)
    solid = rgb + (255,)
    fill = rgb + (int(255 * max(0.0, min(1.0, cfg.opacity))),)
    th = max(1, cfg.thickness)
    fnt = _font(max(14, th * 5))
    for m in marks:
        boxes = [b for b in m.get("boxes", []) if b]
        if not boxes:
            continue
        x0 = min(b[0] for b in boxes) - cfg.pad
        y0 = min(b[1] for b in boxes) - cfg.pad
        x1 = max(b[2] for b in boxes) + cfg.pad
        y1 = max(b[3] for b in boxes) + cfg.pad
        if cfg.style == "highlight":
            d.rectangle([x0, y0, x1, y1], fill=fill)
        elif cfg.style == "ellipse":
            w, h = x1 - x0, y1 - y0
            d.ellipse([x0 - w * 0.08, y0 - h * 0.35, x1 + w * 0.08, y1 + h * 0.35], outline=solid, width=th)
        elif cfg.style == "underline":
            d.line([x0, y1, x1, y1], fill=solid, width=th)
        else:
            d.rectangle([x0, y0, x1, y1], outline=solid, width=th)
        if cfg.char_boxes:
            for c in m.get("chars", []):
                cb = c[1]
                d.rectangle([cb[0], cb[1], cb[2], cb[3]], outline=rgb + (160,), width=1)
        if cfg.label and m.get("label"):
            text = str(m["label"])
            tw = d.textlength(text, font=fnt) if hasattr(d, "textlength") else 8 * len(text)
            th_ = fnt.size + 6 if hasattr(fnt, "size") else 20
            ly = y0 - th_ - 2 if y0 - th_ - 2 > 0 else y1 + 2
            d.rectangle([x0, ly, x0 + tw + 10, ly + th_], fill=solid)
            d.text((x0 + 5, ly + 2), text, fill=(255, 255, 255, 255), font=fnt)
    return Image.alpha_composite(base, ov).convert("RGB")


def save_image(img: Image.Image, path: Path, fmt: str = "png") -> Path:
    path = path.with_suffix("." + ("jpg" if fmt.lower() in ("jpg", "jpeg") else "png"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".jpg":
        img.convert("RGB").save(path, quality=88)
    else:
        img.save(path)
    return path


# ---- synthetic previews for Word / Excel -------------------------------------------------
def synthetic_docx_preview(units: list[Word], highlight_refs: set, labels: dict, cfg: MarkCfg) -> Image.Image:
    W, H, M = 1240, 1754, 90
    fnt = _font(26)
    idx = next((i for i, u in enumerate(units) if u.ref in highlight_refs), 0)
    start = max(0, idx - 8)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    y = M
    marks = []
    for u in units[start:]:
        words, lines, cur = u.text.split(" "), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if d.textlength(t, font=fnt) > W - 2 * M and cur:
                lines.append(cur)
                cur = w
            else:
                cur = t
        lines.append(cur)
        top = y
        for ln in lines:
            if y > H - M:
                break
            d.text((M, y), ln, fill="black", font=fnt)
            y += 38
        if u.ref in highlight_refs:
            marks.append({"boxes": [[M, top, W - M, y - 8]], "label": labels.get(u.ref, "")})
        y += 14
        if y > H - M:
            break
    return draw_marks(img, marks, cfg)


def synthetic_xlsx_preview(cells: list[Word], sheet: str, highlight_refs: set, labels: dict, cfg: MarkCfg) -> Image.Image:
    from openpyxl.utils import get_column_letter
    hl = [c for c in cells if c.ref in highlight_refs]
    r0, c0 = (hl[0].row, hl[0].col) if hl else (0, 0)
    top = max(0, r0 - 4)
    left = 0 if c0 < 8 else c0 - 4
    nrows, ncols = 22, 8
    cw, ch, hx, hy = 220, 44, 70, 60
    img = Image.new("RGB", (hx + ncols * cw, hy + nrows * ch), "white")
    d = ImageDraw.Draw(img)
    fnt, fb = _font(22), _font(20)
    d.text((8, 8), f"Sheet: {sheet}", fill=(60, 60, 60), font=fnt)
    for j in range(ncols):
        x = hx + j * cw
        d.rectangle([x, hy - 30, x + cw, hy], fill=(235, 235, 235), outline=(180, 180, 180))
        d.text((x + cw // 2 - 8, hy - 28), get_column_letter(left + j + 1), fill=(60, 60, 60), font=fb)
    for i in range(nrows):
        y = hy + i * ch
        d.rectangle([0, y, hx, y + ch], fill=(235, 235, 235), outline=(180, 180, 180))
        d.text((10, y + 10), str(top + i + 1), fill=(60, 60, 60), font=fb)
        for j in range(ncols):
            d.rectangle([hx + j * cw, y, hx + (j + 1) * cw, y + ch], outline=(205, 205, 205))
    marks = []
    for c in cells:
        i, j = c.row - top, c.col - left
        if 0 <= i < nrows and 0 <= j < ncols:
            x, y = hx + j * cw, hy + i * ch
            t = c.text if len(c.text) < 22 else c.text[:20] + ".."
            d.text((x + 8, y + 10), t, fill="black", font=fb)
            if c.ref in highlight_refs:
                marks.append({"boxes": [[x, y, x + cw, y + ch]], "label": labels.get(c.ref, ""), })
    return draw_marks(img, marks, cfg)


def libreoffice_preview(path: Path, texts: list[tuple[str, str]], cfg: MarkCfg, dpi: int = 110,
                        tmp: Optional[Path] = None) -> Optional[Image.Image]:
    """Convert Word/Excel to PDF with LibreOffice, find each text, mark it. None if unavailable/not found.
    texts = [(field_label, value_text)]"""
    from .readers import PdfDocument, convert_with_soffice
    tmp = tmp or Path(tempfile.mkdtemp(prefix="dn_lo_"))
    pdf = convert_with_soffice(path, "pdf", tmp)
    if not pdf:
        return None
    try:
        doc = PdfDocument(pdf)
    except Exception:
        return None
    try:
        for label, text in texts[:1]:
            for pi in range(min(doc.page_count, 40)):
                page = doc._page(pi)
                tp = page.get_textpage()
                hit = tp.search(text[:80], match_case=False).get_next()
                if not hit:
                    continue
                idx, count = hit
                img, scale = doc.render(pi, dpi, 25_000_000)
                chars = doc._char_boxes(page, None, 0, scale)
                boxes = [c[1] for c in chars[idx: idx + count] if c]
                marks = [{"boxes": [[min(b[0] for b in boxes), min(b[1] for b in boxes),
                                     max(b[2] for b in boxes), max(b[3] for b in boxes)]], "label": label}]
                return draw_marks(img, marks, cfg)
        return None
    finally:
        doc.close()
