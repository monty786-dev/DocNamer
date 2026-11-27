"""All user-configurable settings as JSON-serialisable dataclasses."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def _filter(cls, data: dict) -> dict:
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in (data or {}).items() if k in names}


@dataclass
class FieldSpec:
    """Common part of a Zone / Anchor: what to read and how to validate it."""
    name: str
    kind: str = "auto"          # printed | handwritten | auto
    fmt: str = "any"            # any | date | number | letters
    regex: str = ""             # optional validation regex (re.search)
    whitelist: str = ""         # allowed characters ("" = everything)
    pages: str = ""             # "" = every selected page, else "1,3-5" (1-based)
    required: bool = True
    default: str = ""


@dataclass
class Zone(FieldSpec):
    """A rectangle in RELATIVE coordinates (0..1) of the upright page."""
    x: float = 0.0
    y: float = 0.0
    w: float = 0.3
    h: float = 0.05
    cell: str = ""              # xlsx/csv: "B4" or "Sheet1!B4" instead of a rectangle

    @classmethod
    def from_dict(cls, d: dict) -> "Zone":
        return cls(**_filter(cls, d))


@dataclass
class Anchor(FieldSpec):
    """Find a keyword, then read to its right / below / in an offset box."""
    keyword: str = ""
    is_regex: bool = False
    case_sensitive: bool = False
    direction: str = "right"    # right | below | offset
    occurrence: int = 1
    max_width: float = 0.5      # right: max distance as fraction of page width
    max_height: float = 3.0     # below: max distance in anchor-heights
    lines_below: int = 1
    offset: list = field(default_factory=lambda: [0.0, 0.0, 0.3, 0.05])  # dx, dy, w, h (page fractions)
    strip_chars: str = ":#-.\u2013\u2014 "

    @classmethod
    def from_dict(cls, d: dict) -> "Anchor":
        return cls(**_filter(cls, d))


@dataclass
class OrientationCfg:
    mode: str = "auto"              # auto | ask | off
    threshold: float = 0.55         # min confidence to rotate by 90/180/270 automatically
    deskew: bool = True
    max_skew: float = 12.0
    min_skew: float = 0.3
    scan_native_pages: bool = False  # also check PDF pages that already have a text layer
    save_corrected_copy: bool = False


@dataclass
class PreprocessCfg:
    mode: str = "auto"              # auto | off | custom
    shadow: bool = False
    denoise: bool = False
    contrast: bool = False
    binarize: bool = False
    border: bool = False


@dataclass
class OcrCfg:
    engine: str = "auto"            # auto | rapid | tesseract
    secondary: str = ""             # ensemble: "" | rapid | tesseract
    languages: str = "eng"          # tesseract language codes ("eng+hin")
    handwriting: str = "auto"       # auto | off
    handwriting_model: str = ""     # folder with rec.onnx + charset.txt  OR a TrOCR folder
    min_conf: float = 0.80
    force_ocr: bool = False
    dpi: int = 300
    max_pixels: int = 40_000_000
    char_level: bool = True
    tesseract_cmd: str = ""
    threads: int = 0


@dataclass
class PageRules:
    pages: str = "first"            # first | last | all | first_found | "1,3-5"
    strategy: str = "first_valid"   # first_match | first_valid | concat | highest_conf | longest
    concat_sep: str = "_"
    skip_blank: bool = True
    blank_threshold: float = 0.003  # fraction of dark pixels below which a page is blank
    skip_keywords: list = field(default_factory=list)   # separator / cover page keywords
    skip_qr: bool = False
    max_pages: int = 20
    on_missing: str = "next_page"   # next_page | fallback | review
    min_conf: float = 0.0           # extra per-field floor (0 = use OCR min_conf)
    must_contain: list = field(default_factory=list)
    must_not_contain: list = field(default_factory=list)
    split_on: str = "none"          # none | blank | keyword
    split_keyword: str = ""


@dataclass
class NamingCfg:
    pattern: str = "{original_name}"
    fallback: str = "{original_name}_unread"
    conflict: str = "suffix"        # suffix | skip | ask
    max_len: int = 150
    date_out: str = "%Y-%m-%d"
    counter_start: int = 1


@dataclass
class MarkCfg:
    enabled: bool = False
    style: str = "rectangle"        # rectangle | ellipse | underline | highlight
    color: str = "#E53935"
    thickness: int = 4
    opacity: float = 0.35
    label: bool = True
    char_boxes: bool = False
    format: str = "png"             # png | jpg
    pad: int = 6
    use_libreoffice: bool = True    # Word/Excel: real page render via LibreOffice if installed


@dataclass
class OutputCfg:
    input_dir: str = ""
    output_dir: str = ""
    recursive: bool = True
    max_depth: int = 0              # 0 = unlimited
    include: list = field(default_factory=lambda: [
        "*.pdf", "*.docx", "*.doc", "*.rtf", "*.xlsx", "*.xlsm", "*.xls", "*.csv",
        "*.jpg", "*.jpeg", "*.png", "*.bmp", "*.tif", "*.tiff", "*.webp", "*.gif",
        "*.heic", "*.heif", "*.jp2", "*.j2k"])
    exclude: list = field(default_factory=list)
    structure: str = "mirror"       # mirror | flat | field:<name>
    save_text: str = "none"         # none | txt | json | both
    searchable_pdf: bool = False
    duplicates: str = "flag"        # flag | skip | keep
    save_corrected_copy: bool = False


@dataclass
class ReviewCfg:
    mode: str = "deferred"          # deferred | interactive | never
    unresolved: str = "review_folder"   # review_folder | fallback | guess
    auto_harvest: bool = False
    teach_handwriting: bool = False     # after a run, ask what handwritten fields say (teaches the library)
    harvest_min_conf: float = 0.97


@dataclass
class Settings:
    mode: str = "zones"             # zones | anchors | full
    zones: list = field(default_factory=list)
    anchors: list = field(default_factory=list)
    auto_template: bool = True
    template: str = ""
    lite: bool = False
    workers: int = 1
    orientation: OrientationCfg = field(default_factory=OrientationCfg)
    preprocess: PreprocessCfg = field(default_factory=PreprocessCfg)
    ocr: OcrCfg = field(default_factory=OcrCfg)
    rules: PageRules = field(default_factory=PageRules)
    naming: NamingCfg = field(default_factory=NamingCfg)
    mark: MarkCfg = field(default_factory=MarkCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    review: ReviewCfg = field(default_factory=ReviewCfg)

    # ---- (de)serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        d = dict(d or {})
        s = cls()
        for f in dataclasses.fields(cls):
            if f.name in ("zones", "anchors") or f.name not in d:
                continue
            cur = getattr(s, f.name)
            if dataclasses.is_dataclass(cur) and isinstance(d[f.name], dict):
                setattr(s, f.name, type(cur)(**_filter(type(cur), d[f.name])))
            else:
                setattr(s, f.name, d[f.name])
        s.zones = [Zone.from_dict(z) if isinstance(z, dict) else z for z in d.get("zones", [])]
        s.anchors = [Anchor.from_dict(a) if isinstance(a, dict) else a for a in d.get("anchors", [])]
        return s

    def save(self, path: Path | str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "Settings":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def fingerprint(self) -> str:
        """Hash of everything that changes the OCR/naming result (not paths)."""
        import hashlib
        d = self.to_dict()
        d.pop("output", None)
        d.pop("lite", None)
        d.pop("workers", None)
        d["output_cfg"] = {k: v for k, v in dataclasses.asdict(self.output).items()
                           if k in ("structure", "duplicates")}
        raw = json.dumps(d, sort_keys=True, default=str).encode()
        return hashlib.sha1(raw).hexdigest()[:16]

    def apply_lite_defaults(self) -> None:
        """Low-RAM / CPU-only defaults used by the Winlator build."""
        self.lite = True
        self.workers = 1
        self.ocr.dpi = min(self.ocr.dpi, 200)
        self.ocr.max_pixels = min(self.ocr.max_pixels, 12_000_000)
        self.ocr.secondary = ""
        self.ocr.threads = self.ocr.threads or 2
        self.mark.format = "jpg"
        self.preprocess.denoise = False


@dataclass
class Template:
    """A reusable bundle: zones/anchors + naming + page rules + matching info."""
    name: str
    mode: str = "zones"
    zones: list = field(default_factory=list)
    anchors: list = field(default_factory=list)
    pattern: str = "{original_name}"
    fallback: str = "{original_name}_unread"
    rules: Optional[dict] = None
    match_keywords: list = field(default_factory=list)
    match_any: bool = False
    layout_sig: list = field(default_factory=list)    # 32x32 thumbnail of an example first page
    layout_threshold: float = 0.85

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Template":
        t = cls(**_filter(cls, d))
        t.zones = [Zone.from_dict(z) if isinstance(z, dict) else z for z in t.zones]
        t.anchors = [Anchor.from_dict(a) if isinstance(a, dict) else a for a in t.anchors]
        return t

    def apply_to(self, s: Settings) -> Settings:
        """Return a copy of settings with this template's fields applied."""
        s2 = Settings.from_dict(s.to_dict())
        s2.mode = self.mode
        s2.zones = [Zone.from_dict(dataclasses.asdict(z)) for z in self.zones]
        s2.anchors = [Anchor.from_dict(dataclasses.asdict(a)) for a in self.anchors]
        s2.naming.pattern = self.pattern
        s2.naming.fallback = self.fallback
        if self.rules:
            s2.rules = PageRules(**_filter(PageRules, self.rules))
        return s2

    def save(self, path: Path | str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "Template":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
