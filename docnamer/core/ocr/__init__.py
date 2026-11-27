"""Engine manager: picks printed / handwriting engines, runs the optional ensemble."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from PIL import Image

from ..layout import words_to_text
from ..models import Word
from ..settings import OcrCfg
from .base import OcrEngine

log = logging.getLogger("docnamer.ocr")


def mean_conf(words: list[Word]) -> float:
    tot = sum(max(len(w.text), 1) for w in words)
    if not tot:
        return 0.0
    return sum(w.conf * max(len(w.text), 1) for w in words) / tot


class Engines:
    def __init__(self, cfg: OcrCfg, data_dir: Path | str = "", library=None):
        self.cfg = cfg
        self.library = library                    # taught handwriting samples (core.library.Library)
        self.data_dir = Path(data_dir) if data_dir else None
        self.printed: Optional[OcrEngine] = None
        self.secondary: Optional[OcrEngine] = None
        self.hw: Optional[OcrEngine] = None
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._built = False

    # -- construction ----------------------------------------------------------
    def _make(self, name: str) -> Optional[OcrEngine]:
        c = self.cfg
        try:
            if name == "rapid":
                from .rapid_engine import RapidOcrEngine
                if not RapidOcrEngine.available():
                    raise ImportError("rapidocr_onnxruntime not installed")
                md = str(self.data_dir / "models" / "rapid") if self.data_dir and (self.data_dir / "models" / "rapid").exists() else ""
                return RapidOcrEngine(threads=c.threads, model_dir=md, char_level=c.char_level)
            if name == "tesseract":
                from .tesseract_engine import TesseractEngine
                td = self.data_dir / "models" / "tessdata" if self.data_dir else None
                return TesseractEngine(c.languages, c.tesseract_cmd, c.char_level, c.threads,
                                       str(td) if td and td.exists() else "")
        except Exception as e:
            self.errors.append(f"{name}: {e}")
            log.warning("OCR engine %s unavailable: %s", name, e)
        return None

    def _make_hw(self) -> Optional[OcrEngine]:
        if self.cfg.handwriting == "off":
            return None
        d = self.cfg.handwriting_model or (str(self.data_dir / "models" / "handwriting") if self.data_dir else "")
        if not d or not Path(d).exists():
            return None
        try:
            from .handwriting import CtcOnnxRecognizer, TrocrEngine
            if CtcOnnxRecognizer.available(d):
                return CtcOnnxRecognizer(d, threads=self.cfg.threads)
            if TrocrEngine.available(d):
                return TrocrEngine(d, threads=self.cfg.threads)
            self.errors.append(f"handwriting model in {d} not recognised")
        except Exception as e:
            self.errors.append(f"handwriting: {e}")
            log.warning("handwriting engine unavailable: %s", e)
        return None

    def build(self) -> "Engines":
        with self._lock:
            if self._built:
                return self
            order = {"auto": ["rapid", "tesseract"], "rapid": ["rapid", "tesseract"],
                     "tesseract": ["tesseract", "rapid"]}.get(self.cfg.engine, ["rapid", "tesseract"])
            if self.needs_devanagari() and not self.rapid_has_devanagari():
                # the bundled RapidOCR models cannot read Devanagari: never route Hindi to them
                order = ["tesseract"]
            for n in order:
                self.printed = self._make(n)
                if self.printed:
                    break
            if self.cfg.secondary and self.printed and self.cfg.secondary != self.printed.name.replace("ocr", ""):
                self.secondary = self._make(self.cfg.secondary)
            self.hw = self._make_hw()
            self._built = True
        return self

    def needs_devanagari(self) -> bool:
        return any(l in ("hin", "mar", "san", "nep", "Devanagari") for l in self.cfg.languages.split("+"))

    def rapid_has_devanagari(self) -> bool:
        """True only if the user installed a Devanagari recognition model for RapidOCR."""
        return bool(self.data_dir and (self.data_dir / "models" / "rapid_hin" / "rec.onnx").exists())

    def available(self) -> bool:
        self.build()
        return self.printed is not None

    def hw_engines(self) -> list:
        """Handwriting engines: ONNX CTC / TrOCR model (if installed) + the user's taught library."""
        self.build()
        out = [self.hw] if self.hw else []
        if self.cfg.handwriting != "off":
            from .library_engine import LibraryEngine
            if LibraryEngine.available(self.library):
                out.append(LibraryEngine(self.library))
        return out

    def has_handwriting(self) -> bool:
        self.build()
        return self.hw is not None

    # -- recognition ---------------------------------------------------------------
    def recognize(self, img: Image.Image, kind: str = "printed", whitelist: str = "", fast: bool = False,
                  psm: int = 0, min_conf: Optional[float] = None) -> tuple[list[Word], list]:
        """Returns (best words, candidates [[text, conf, engine], ...])."""
        self.build()
        thr = self.cfg.min_conf if min_conf is None else min_conf
        cands: list = []
        results: list[tuple[list[Word], str]] = []

        def run(e: OcrEngine, label: str = ""):
            try:
                ws = e.recognize(img, whitelist=whitelist, fast=fast, psm=psm)
            except Exception as ex:
                log.warning("%s failed: %s", e.name, ex)
                return None
            t = words_to_text(ws, " ")
            cands.append([t, round(mean_conf(ws), 4), e.name])
            results.append((ws, e.name))
            return ws

        hws = [] if fast else self.hw_engines()
        if kind == "handwritten" and hws:
            for e in hws:
                run(e)
            if (not results or not any(r[0] for r in results)) and self.printed:
                run(self.printed)
        else:
            if self.printed:
                run(self.printed)
            if kind == "auto" and hws and (not results or mean_conf(results[0][0]) < thr):
                for e in hws:
                    run(e)
            if self.secondary and not fast:
                run(self.secondary)
        if not results:
            return [], cands
        best = max(results, key=lambda r: mean_conf(r[0]) if r[0] else -1)[0]
        return best, cands
