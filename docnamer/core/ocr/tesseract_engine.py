"""Tesseract engine (optional; also used for orientation OSD)."""
from __future__ import annotations

from PIL import Image, ImageOps

from ..env import find_tesseract
from ..models import Word
from .base import OcrEngine


class TesseractEngine(OcrEngine):
    name = "tesseract"

    def __init__(self, lang: str = "eng", cmd: str = "", char_level: bool = True, threads: int = 0,
                 tessdata_dir: str = ""):
        import os
        import pytesseract
        self.pt = pytesseract
        c = find_tesseract(cmd)
        if not c:
            raise RuntimeError("Tesseract executable not found")
        pytesseract.pytesseract.tesseract_cmd = c
        if tessdata_dir and any(os.scandir(tessdata_dir)):      # portable language packs next to the app
            os.environ["TESSDATA_PREFIX"] = tessdata_dir
        self.lang = lang or "eng"
        have = set(pytesseract.get_languages(config=""))
        missing = [l for l in self.lang.split("+") if l and l not in have]
        if missing:
            raise RuntimeError(
                f"Tesseract language data missing: {', '.join(missing)}. Run  python scripts/fetch_models.py --tess {','.join(missing)}"
                f"  (or copy <lang>.traineddata into the tessdata folder).")
        self.char_level = char_level
        self.threads = threads

    @staticmethod
    def available(cmd: str = "") -> bool:
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            return False
        return find_tesseract(cmd) is not None

    def _prep(self, img: Image.Image) -> tuple[Image.Image, float, int]:
        w, h = img.size
        scale = 1.0
        if h < 60:
            scale = min(4.0, 60.0 / max(h, 1))
        elif h < 120 and w < 1500:
            scale = 2.0
        if scale != 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        pad = 12
        img = ImageOps.expand(img.convert("RGB"), border=pad, fill="white")
        return img, scale, pad

    def recognize(self, img: Image.Image, whitelist: str = "", fast: bool = False, psm: int = 0) -> list[Word]:
        pt = self.pt
        big_page = img.size[0] * img.size[1] > 1_500_000
        prepped, scale, pad = (img.convert("RGB"), 1.0, 0) if big_page else self._prep(img)
        psm = psm or (3 if big_page else 6)
        cfg = f"--oem 1 --psm {psm}"
        if whitelist and whitelist.isalnum():
            cfg += f" -c tessedit_char_whitelist={whitelist}"
        d = pt.image_to_data(prepped, lang=self.lang, config=cfg, output_type=pt.Output.DICT)
        words: list[Word] = []
        for i, t in enumerate(d["text"]):
            t = (t or "").strip()
            if not t:
                continue
            try:
                conf = float(d["conf"][i])
            except (TypeError, ValueError):
                continue
            if conf < 0:
                continue
            l, tp, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            box = [(l - pad) / scale, (tp - pad) / scale, (l + w - pad) / scale, (tp + h - pad) / scale]
            words.append(Word(text=t, box=box, conf=conf / 100.0, source=self.name))
        if self.char_level and words and not fast:
            self._attach_chars(prepped, words, scale, pad, cfg)
        return words

    def _attach_chars(self, prepped, words, scale, pad, cfg):
        pt = self.pt
        try:
            raw = pt.image_to_boxes(prepped, lang=self.lang, config=cfg)
        except Exception:
            return
        H = prepped.size[1]
        for line in raw.splitlines():
            p = line.split(" ")
            if len(p) < 5:
                continue
            ch = p[0]
            l, b, r, t = (int(v) for v in p[1:5])
            box = [(l - pad) / scale, (H - t - pad) / scale, (r - pad) / scale, (H - b - pad) / scale]
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            for w in words:
                if w.box[0] - 2 <= cx <= w.box[2] + 2 and w.box[1] - 2 <= cy <= w.box[3] + 2:
                    if w.chars is None:
                        w.chars = []
                    w.chars.append([ch, box, w.conf])
                    break
