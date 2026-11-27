"""Plain data objects passed between engine stages (all JSON-serialisable)."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Word:
    """A word / text-run / cell. Coordinates are pixels of the working page image."""
    text: str
    box: Optional[list] = None          # [x0, y0, x1, y1] or None (docx / xlsx units)
    conf: float = 1.0
    source: str = "native"              # native | rapidocr | tesseract | handwriting | ...
    ref: str = ""                       # cell address ("Sheet1!B4") or paragraph id ("p12")
    row: int = -1
    col: int = -1
    para: int = -1
    chars: Optional[list] = None        # [[char, [x0,y0,x1,y1], conf], ...]

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2 if self.box else 0.0

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2 if self.box else 0.0

    @property
    def h(self) -> float:
        return (self.box[3] - self.box[1]) if self.box else 0.0


@dataclass
class FieldValue:
    name: str
    text: str = ""
    conf: float = 0.0
    valid: bool = True
    found: bool = False
    page: int = -1
    box: Optional[list] = None
    boxes: list = field(default_factory=list)
    chars: list = field(default_factory=list)
    ref: str = ""
    source: str = ""
    candidates: list = field(default_factory=list)   # [[text, conf, source], ...]
    needs_review: bool = False
    reviewed: bool = False
    raw: str = ""
    crop_path: str = ""
    note: str = ""
    required: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "FieldValue":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class ReviewItem:
    id: str
    path: str                 # original file path
    field: str                # field name, or "__orientation__"
    page: int = 0
    text: str = ""
    conf: float = 0.0
    crop_path: str = ""
    candidates: list = field(default_factory=list)     # [[text, conf, source], ...]
    reason: str = ""
    kind: str = "field"       # field | orientation

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewItem":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class FileResult:
    path: str
    rel: str = ""                       # path relative to the input root
    sha: str = ""
    kind: str = ""                      # pdf | image | docx | xlsx | csv
    source_file: str = ""               # file that will be copied (original, or a split segment)
    segment: int = -1
    seg_pages: list = field(default_factory=list)
    status: str = "pending"             # pending | ok | review | fallback | manual | duplicate | skipped | error | encrypted | done
    message: str = ""
    template: str = ""
    fields: dict = field(default_factory=dict)          # name -> FieldValue
    new_name: str = ""                  # file name incl. extension
    name_locked: bool = False
    dest_dir: str = ""
    new_path: str = ""
    page_transforms: dict = field(default_factory=dict)  # page -> {"rot": cw, "skew": deg}
    marks: list = field(default_factory=list)            # [{"page":0,"field":"x","boxes":[...],"chars":[...]}]
    review: list = field(default_factory=list)           # list[ReviewItem]
    teach: list = field(default_factory=list)            # list[ReviewItem] (kind="teach"): handwriting to learn from
    engine: str = ""
    confidence: float = 0.0
    pages_read: list = field(default_factory=list)
    force_fallback: bool = False
    pattern: str = ""
    fallback: str = ""
    counter: int = 0
    marked_paths: list = field(default_factory=list)
    text_path: str = ""
    json_path: str = ""
    pdf_path: str = ""
    duplicate_of: str = ""
    tmp_text: str = ""
    tmp_json: str = ""
    tmp_pdf: str = ""

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FileResult":
        names = {f.name for f in dataclasses.fields(cls)}
        r = cls(**{k: v for k, v in d.items() if k in names})
        r.fields = {k: (v if isinstance(v, FieldValue) else FieldValue.from_dict(v))
                    for k, v in (r.fields or {}).items()}
        r.review = [x if isinstance(x, ReviewItem) else ReviewItem.from_dict(x) for x in (r.review or [])]
        r.teach = [x if isinstance(x, ReviewItem) else ReviewItem.from_dict(x) for x in (r.teach or [])]
        r.page_transforms = {int(k): v for k, v in (r.page_transforms or {}).items()}
        return r

    def field_text(self) -> dict:
        return {k: v.text for k, v in self.fields.items()}
