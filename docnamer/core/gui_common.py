"""Toolkit-neutral GUI description shared by the Qt (full) and Tk (lite/Winlator) front-ends.

Each wizard step is a list of Field rows: (settings_path, label, kind, extra).
kind:  str | dir | bool | int | float | choice | choiceedit | list | color
extra: choices (list) for choice kinds, (min, max) for numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .i18n import t


@dataclass
class Field:
    path: str
    label: str
    kind: str = "str"
    extra: Any = None
    tip: str = ""


def get_path(obj, path: str):
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def set_path(obj, path: str, value) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        obj = getattr(obj, p)
    setattr(obj, parts[-1], value)


def to_text(value, kind: str) -> str:
    if kind == "list":
        return ", ".join(value or [])
    if kind == "bool":
        return "1" if value else "0"
    return "" if value is None else str(value)


def from_text(text: str, kind: str, old=None):
    text = (text or "").strip()
    if kind == "list":
        return [p.strip() for p in text.split(",") if p.strip()]
    if kind == "bool":
        return text in ("1", "true", "True", "yes")
    if kind == "int":
        try:
            return int(float(text))
        except ValueError:
            return old
    if kind == "float":
        try:
            return float(text)
        except ValueError:
            return old
    return text


def steps() -> dict[str, list[Field]]:
    return {
        "folder": [
            Field("output.input_dir", t("input_folder"), "dir"),
            Field("output.output_dir", t("output_folder"), "dir"),
            Field("output.recursive", t("include_sub"), "bool"),
            Field("output.max_depth", "Max subfolder depth (0 = unlimited)", "int", (0, 50)),
            Field("output.include", "Include patterns (comma separated)", "list"),
            Field("output.exclude", "Exclude patterns (comma separated)", "list"),
            Field("output.structure", "Output structure", "choiceedit", ["mirror", "flat", "field:invoice"],
                  "mirror = keep sub-folders, flat = one folder, field:<name> = group by a field's value"),
            Field("output.duplicates", "Identical files (same content)", "choice", ["flag", "skip", "keep"]),
            Field("ocr.engine", "OCR engine", "choice", ["auto", "rapid", "tesseract"]),
            Field("ocr.languages", "Text language (hin = Hindi / \u0939\u093f\u0928\u094d\u0926\u0940, eng = English)", "choiceedit",
                  ["eng", "hin", "eng+hin"], "Hindi needs hin.traineddata - see scripts/fetch_models.py"),
        ],
        "orientation": [
            Field("orientation.mode", t("orientation_auto"), "choice", ["auto", "ask", "off"],
                  "auto = fix silently; ask = queue uncertain pages for you; off = never rotate"),
            Field("orientation.threshold", "Confidence needed to rotate automatically", "float", (0.0, 1.0)),
            Field("orientation.deskew", "Also straighten small tilts (deskew)", "bool"),
            Field("orientation.max_skew", "Max tilt to correct (degrees)", "float", (1, 45)),
            Field("orientation.scan_native_pages", "Also check pages that already have a text layer", "bool"),
            Field("orientation.save_corrected_copy", "Also save an orientation-corrected copy", "bool"),
            Field("preprocess.mode", "Image clean-up", "choice", ["auto", "off", "custom"]),
            Field("preprocess.shadow", "  custom: remove shadows", "bool"),
            Field("preprocess.denoise", "  custom: denoise", "bool"),
            Field("preprocess.contrast", "  custom: boost contrast", "bool"),
            Field("preprocess.binarize", "  custom: black & white", "bool"),
            Field("preprocess.border", "  custom: remove dark scan borders", "bool"),
            Field("ocr.dpi", "Render resolution (DPI)", "int", (72, 600)),
            Field("ocr.force_ocr", "Force OCR even when a PDF has a text layer", "bool"),
        ],
        "pages": [
            Field("rules.pages", "Pages to read", "choiceedit", ["first", "last", "all", "first_found", "1,3-5"],
                  "first | last | all | first_found | a list such as 1,3-5"),
            Field("rules.strategy", "When several pages give a value", "choice",
                  ["first_match", "first_valid", "highest_conf", "longest", "concat"]),
            Field("rules.on_missing", "If a field is not found", "choice", ["next_page", "fallback", "review"]),
            Field("rules.max_pages", "Max pages to scan per document", "int", (1, 500)),
            Field("rules.skip_blank", "Skip blank pages", "bool"),
            Field("rules.skip_keywords", "Skip cover/separator pages containing (comma separated)", "list"),
            Field("rules.skip_qr", "Skip pages with a QR code", "bool"),
            Field("rules.min_conf", "Extra minimum confidence (0 = use OCR threshold)", "float", (0.0, 1.0)),
            Field("rules.must_contain", "Value must contain (comma separated)", "list"),
            Field("rules.must_not_contain", "Value must NOT contain (comma separated)", "list"),
            Field("rules.split_on", "Split multi-document PDFs at", "choice", ["none", "blank", "keyword"]),
            Field("rules.split_keyword", "Separator keyword", "str"),
            Field("ocr.min_conf", "OCR confidence below which I am asked", "float", (0.0, 1.0)),
            Field("review.mode", "When unsure", "choice", ["deferred", "interactive", "never"],
                  "deferred = review queue at the end; interactive = ask immediately; never = use fallback name"),
            Field("review.unresolved", "Unresolved items", "choice", ["review_folder", "fallback", "guess"]),
            Field("ocr.handwriting", "Handwriting recognition", "choice", ["auto", "off"]),
            Field("ocr.handwriting_model", "Handwriting model folder (rec.onnx + charset.txt)", "dir"),
            Field("review.auto_harvest", "Save confident OCR crops to the font library", "bool"),
            Field("review.teach_handwriting", "After the run, ask me what handwritten fields say (teach handwriting)", "bool"),
        ],
        "naming": [
            Field("naming.pattern", t("pattern"), "str", None,
                  "Tokens: {fieldname} {original_name} {date} {mdate} {page} {counter:03} {folder} {first_line}"),
            Field("naming.fallback", t("fallback"), "str"),
            Field("naming.conflict", "If the name exists", "choice", ["suffix", "skip", "ask"]),
            Field("naming.max_len", "Max name length", "int", (20, 200)),
            Field("naming.date_out", "Default date format", "str"),
            Field("mark.enabled", "Save a marked image per document", "bool"),
            Field("mark.style", "Mark style", "choice", ["rectangle", "ellipse", "underline", "highlight"]),
            Field("mark.color", "Mark colour (#RRGGBB)", "color"),
            Field("mark.thickness", "Line thickness", "int", (1, 20)),
            Field("mark.opacity", "Highlight opacity", "float", (0.05, 1.0)),
            Field("mark.label", "Write the field name on the mark", "bool"),
            Field("mark.char_boxes", "Also box every character", "bool"),
            Field("mark.format", "Marked image format", "choice", ["png", "jpg"]),
            Field("output.save_text", "Save full text", "choice", ["none", "txt", "json", "both"]),
            Field("output.searchable_pdf", "Also make searchable PDFs (Latin text)", "bool"),
        ],
    }


NAMING_HELP = (
    "Transforms:  {inv|upper}  {name|nospace}  {d|date:%Y%m%d}  {x|re:(\\d+)}  {y|default:NA}  "
    "{z|replace:/=-}  {w|max:20}  {v|prefix:INV-}"
)
