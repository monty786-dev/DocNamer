"""Template store + automatic template matching (keywords or layout similarity)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from .settings import Template


def layout_signature(img: Image.Image) -> list:
    g = img.convert("L").resize((32, 32), Image.BOX)
    return (255 - np.asarray(g, dtype=np.int32)).flatten().tolist()


def layout_similarity(a: list, b: list) -> float:
    x, y = np.asarray(a, np.float64), np.asarray(b, np.float64)
    x, y = x - x.mean(), y - y.mean()
    d = np.linalg.norm(x) * np.linalg.norm(y)
    return float((x @ y) / d) if d > 0 else 0.0


class TemplateManager:
    def __init__(self, folder: Path | str):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def load(self, name: str) -> Template:
        return Template.load(self.dir / f"{name}.json")

    def save(self, t: Template) -> Path:
        p = self.dir / f"{t.name}.json"
        t.save(p)
        return p

    def delete(self, name: str) -> None:
        (self.dir / f"{name}.json").unlink(missing_ok=True)

    def all(self) -> list[Template]:
        out = []
        for n in self.names():
            try:
                out.append(self.load(n))
            except Exception:
                continue
        return out

    def needs_text(self) -> bool:
        return any(t.match_keywords for t in self.all())

    def match(self, first_page_text: str, first_page_img: Optional[Image.Image]) -> Optional[Template]:
        low = (first_page_text or "").lower()
        sig = layout_signature(first_page_img) if first_page_img is not None else None
        best, best_score = None, 0.0
        for t in self.all():
            score = 0.0
            if t.match_keywords:
                hits = sum(1 for k in t.match_keywords if k.lower() in low)
                ok = hits > 0 if t.match_any else hits == len(t.match_keywords)
                if ok:
                    score = 1.0 + hits
            elif t.layout_sig and sig is not None:
                s = layout_similarity(sig, t.layout_sig)
                if s >= t.layout_threshold:
                    score = s
            if score > best_score:
                best, best_score = t, score
        return best
