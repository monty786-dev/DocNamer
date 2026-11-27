"""Full-text outputs: UTF-8 .txt, JSON (words + per-character boxes + confidence), searchable PDF."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Optional

from PIL import Image

from .models import Word


def words_json(words: list[Word]) -> list[dict]:
    out = []
    for w in words:
        d = {"text": w.text, "conf": round(w.conf, 4), "source": w.source}
        if w.box:
            d["box"] = [round(v, 1) for v in w.box]
        if w.ref:
            d["ref"] = w.ref
        if w.chars:
            d["chars"] = [{"c": c[0], "box": [round(v, 1) for v in c[1]], "conf": round(float(c[2]), 4)} for c in w.chars]
        out.append(d)
    return out


def write_text_outputs(pages: list[dict], base: Path, want_txt: bool, want_json: bool) -> tuple[str, str]:
    """pages: [{"page": 1, "label": "...", "text": "...", "words": [...]}]. Returns (txt_path, json_path)."""
    base.parent.mkdir(parents=True, exist_ok=True)
    txt = js = ""
    if want_txt:
        parts = []
        for p in pages:
            parts.append(f"===== Page {p['page']}" + (f" ({p['label']})" if p.get("label") and str(p["label"]) != str(p["page"]) else "") + " =====")
            parts.append(p["text"])
        base.with_suffix(".txt").write_text("\n".join(parts), encoding="utf-8")
        txt = str(base.with_suffix(".txt"))
    if want_json:
        base.with_suffix(".json").write_text(json.dumps({"pages": pages}, ensure_ascii=False, indent=1), encoding="utf-8")
        js = str(base.with_suffix(".json"))
    return txt, js


class SearchablePdfWriter:
    """Page image + invisible text layer (reportlab). Latin-1 text only: other scripts are dropped
    from the hidden layer (the visible image is unaffected)."""

    def __init__(self, out: Path):
        from reportlab.pdfgen import canvas
        self.out = out
        out.parent.mkdir(parents=True, exist_ok=True)
        self.c = canvas.Canvas(str(out))
        self.n = 0

    def add_page(self, img: Image.Image, words: list[Word], px_per_pt: float) -> None:
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase.pdfmetrics import stringWidth
        W, H = img.size
        wp, hp = W / px_per_pt, H / px_per_pt
        self.c.setPageSize((wp, hp))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=85)
        buf.seek(0)
        self.c.drawImage(ImageReader(buf), 0, 0, wp, hp)
        for w in words:
            if not w.box or not w.text:
                continue
            t = w.text.encode("latin-1", "ignore").decode("latin-1")
            if not t:
                continue
            x0, y0, x1, y1 = (v / px_per_pt for v in w.box)
            fs = max(4.0, (y1 - y0) * 0.9)
            sw = stringWidth(t, "Helvetica", fs)
            to = self.c.beginText()
            to.setTextRenderMode(3)
            to.setFont("Helvetica", fs)
            to.setHorizScale(100.0 * (x1 - x0) / sw if sw > 0 else 100.0)
            to.setTextOrigin(x0, hp - y1 + (y1 - y0) * 0.2)
            to.textOut(t)
            self.c.drawText(to)
        self.c.showPage()
        self.n += 1

    def save(self) -> Optional[str]:
        if self.n:
            self.c.save()
            return str(self.out)
        return None
