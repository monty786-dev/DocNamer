"""'Teach' flow: cut a handwriting crop into words (shabd) and letters (akshar), ask the user what
each piece contains, and store the answers in the library for future documents."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

from .library import Library
from .segment import deskew, segment_letters, segment_lines, segment_words, tight_y
from .textutil import graphemes, script_of


@dataclass
class Segment:
    unit: str                       # word | char
    box: tuple                      # (x0, y0, x1, y1) in the source crop
    crop: Image.Image
    guess: str = ""
    guess_conf: float = 0.0
    parent: int = -1                # for letters: index of the word segment
    label: str = ""
    saved: bool = False


def analyze(img: Image.Image, lib: Optional[Library] = None, hint_text: str = "") -> list[Segment]:
    """Words first (each followed by its letters). `hint_text` ('रमेश कुमार') pre-fills the guesses
    when it has the same number of words as were found."""
    segs: list[Segment] = []
    words_idx: list[int] = []
    img = deskew(img)
    for (y0, y1) in segment_lines(img):
        line = img.crop((0, y0, img.size[0], y1))
        for (x0, x1) in segment_words(line):
            wimg = line.crop((x0, 0, x1, line.size[1]))
            ty0, ty1 = tight_y(line, x0, x1)
            wimg = wimg.crop((0, max(0, ty0 - 2), wimg.size[0], min(wimg.size[1], ty1 + 2)))
            segs.append(Segment("word", (x0, y0 + ty0, x1, y0 + ty1), wimg))
            wi = len(segs) - 1
            words_idx.append(wi)
            for (a, b) in segment_letters(wimg):
                segs.append(Segment("char", (x0 + a, y0 + ty0, x0 + b, y0 + ty1), wimg.crop((a, 0, b, wimg.size[1])), parent=wi))
    hint = hint_text.split()
    for k, wi in enumerate(words_idx):
        s = segs[wi]
        if lib is not None:
            m = lib.suggest(s.crop, 1, 0.78, "word")
            if m:
                s.guess, s.guess_conf = m[0][0], m[0][1]
        if not s.guess and len(hint) == len(words_idx):
            s.guess, s.guess_conf = hint[k], 0.5
    if lib is not None:
        for s in segs:
            if s.unit == "char":
                m = lib.suggest(s.crop, 1, 0.75, "char")
                if m:
                    s.guess, s.guess_conf = m[0][0], m[0][1]
    return segs


class TeachSession:
    """Drives the ask-the-user loop. GUIs call current() -> show crop -> save()/skip()/back()."""

    def __init__(self, lib: Library, img: Image.Image, source_doc: str = "", page: int = 0, profile: str = "default",
                 kind: str = "handwriting", mode: str = "words", hint_text: str = ""):
        self.lib, self.source_doc, self.page, self.profile, self.kind = lib, source_doc, page, profile, kind
        self.segments = analyze(img, lib, hint_text)
        want = {"words": ("word",), "letters": ("char",), "both": ("word", "char")}[mode]
        self.order = [i for i, s in enumerate(self.segments) if s.unit in want]
        self.pos = 0
        self.saved_ids: list[int] = []

    # ---- navigation ------------------------------------------------------------------------
    @property
    def finished(self) -> bool:
        return self.pos >= len(self.order)

    @property
    def total(self) -> int:
        return len(self.order)

    def current(self) -> Optional[Segment]:
        return None if self.finished else self.segments[self.order[self.pos]]

    def skip(self) -> None:
        self.pos += 1

    def back(self) -> None:
        self.pos = max(0, self.pos - 1)

    def letters_of_current(self) -> list[int]:
        cur = self.order[self.pos]
        return [i for i, s in enumerate(self.segments) if s.unit == "char" and s.parent == cur]

    def ask_letters_next(self) -> int:
        """Insert this word's letter pieces right after it in the queue. Returns how many were added."""
        kids = [i for i in self.letters_of_current() if i not in self.order]
        self.order[self.pos + 1:self.pos + 1] = kids
        return len(kids)

    # ---- saving ------------------------------------------------------------------------------------
    def _add(self, seg: Segment, label: str, unit: str) -> int:
        sid = self.lib.add_sample(seg.crop, label, self.kind, self.profile, self.source_doc, self.page,
                                  list(seg.box), seg.guess_conf, True, unit=unit)
        seg.label, seg.saved = label, True
        self.saved_ids.append(sid)
        return sid

    def save(self, label: str, also_letters: bool = False) -> dict:
        """Save the current piece with the label the user typed. For a word, optionally also save its
        letters when their count matches the number of aksharas typed."""
        seg = self.current()
        label = label.strip()
        info = {"saved": 0, "letters_saved": 0, "letter_mismatch": None}
        if seg is None or not label:
            return info
        self._add(seg, label, seg.unit)
        info["saved"] = 1
        if also_letters and seg.unit == "word":
            kids = self.letters_of_current()
            g = graphemes(label)
            if kids and len(kids) == len(g):
                for ki, gr in zip(kids, g):
                    self._add(self.segments[ki], gr, "char")
                    info["letters_saved"] += 1
            else:
                info["letter_mismatch"] = (len(kids), len(g))
        self.pos += 1
        return info


def auto_align(lib: Library, img: Image.Image, text: str, kind: str = "handwriting", profile: str = "default",
               source_doc: str = "", page: int = 0, also_letters: bool = True) -> dict:
    """User typed `text` for a crop: if the crop has exactly as many words, save each word (and its
    letters when counts match) WITHOUT asking. Otherwise report a mismatch so the GUI can ask."""
    words = text.split()
    sess = TeachSession(lib, img, source_doc, page, profile, kind, "words", hint_text=text)
    n_words = sum(1 for s in sess.segments if s.unit == "word")
    if not words or n_words != len(words):
        return {"saved": 0, "letters_saved": 0, "mismatch": True, "found_words": n_words, "typed_words": len(words)}
    saved = letters = 0
    for w in words:
        r = sess.save(w, also_letters)
        saved += r["saved"]
        letters += r["letters_saved"]
    return {"saved": saved, "letters_saved": letters, "mismatch": False, "found_words": n_words, "typed_words": len(words)}
