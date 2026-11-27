"""File-name building: tokens, transforms, date normalisation, Windows-safe sanitising."""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .textutil import normalize_date_text, to_ascii_digits, transliterate

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
TOKEN = re.compile(r"\{([^{}]+)\}")
MAX_FULL_PATH = 240

DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%m-%d-%Y", "%m/%d/%Y",
    "%d-%m-%y", "%d/%m/%y", "%d.%m.%y", "%Y%m%d", "%d%m%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y",
    "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y", "%Y-%m-%d %H:%M:%S",
]


def parse_date(text: str) -> Optional[datetime]:
    s = re.sub(r"\s+", " ", normalize_date_text(text or "").strip().strip(".,;:"))
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    if not s:
        return None
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def sanitize_component(s: str, max_len: int = 150) -> str:
    s = unicodedata.normalize("NFC", s or "")
    s = ILLEGAL.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")
    if s.split(".")[0].upper() in RESERVED:
        s = "_" + s
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    return s


def _split_ops(token: str) -> list[str]:
    return [p.replace("\\|", "|") for p in re.split(r"(?<!\\)\|", token)]


def apply_ops(value: str, ops: list[str], date_out: str = "%Y-%m-%d") -> str:
    v = value or ""
    for op in ops:
        op = op.strip()
        name, _, arg = op.partition(":")
        name = name.strip().lower()
        if name == "upper":
            v = v.upper()
        elif name == "lower":
            v = v.lower()
        elif name == "title":
            v = v.title()
        elif name == "trim":
            v = v.strip()
        elif name == "nospace":
            v = re.sub(r"\s+", "_", v.strip())
        elif name == "digits":
            v = re.sub(r"\D", "", to_ascii_digits(v))
        elif name == "asciidigits":
            v = to_ascii_digits(v)
        elif name == "translit":
            v = transliterate(v)
        elif name == "alnum":
            v = re.sub(r"[^\w]", "", v, flags=re.UNICODE)
        elif name == "replace":
            a, _, b = arg.partition("=")
            v = v.replace(a, b)
        elif name == "re":
            try:
                m = re.search(arg, v)
                v = (m.group(1) if m and m.groups() else m.group(0)) if m else ""
            except re.error:
                pass
        elif name == "date":
            d = parse_date(v)
            v = d.strftime(arg or date_out) if d else ""
        elif name == "prefix":
            v = (arg + v) if v else v
        elif name == "suffix":
            v = (v + arg) if v else v
        elif name == "max":
            v = v[:int(arg)] if arg.isdigit() else v
        elif name == "left":
            v = v[:int(arg)] if arg.isdigit() else v
        elif name == "right":
            v = v[-int(arg):] if arg.isdigit() else v
        elif name == "default":
            v = v if v.strip() else arg
    return v


def referenced_fields(pattern: str) -> list[str]:
    builtin = {"original_name", "date", "mdate", "page", "counter", "folder", "ext", "first_line", "text_snippet"}
    out = []
    for m in TOKEN.finditer(pattern):
        base = _split_ops(m.group(1))[0].split(":")[0].strip()
        if base and base not in builtin and base not in out:
            out.append(base)
    return out


def render_pattern(pattern: str, fields: dict[str, str], ctx: dict, date_out: str = "%Y-%m-%d"
                   ) -> tuple[str, list[str]]:
    """Render a pattern. Returns (stem, missing_field_names)."""
    missing: list[str] = []

    def repl(m: re.Match) -> str:
        parts = _split_ops(m.group(1))
        head, ops = parts[0].strip(), parts[1:]
        base, _, spec = head.partition(":")
        base = base.strip()
        if base == "counter":
            n = int(ctx.get("counter", 1))
            return str(n).zfill(int(spec)) if spec.isdigit() else str(n)
        if base in fields:                       # a user field always wins over a built-in token
            out = apply_ops(fields[base], ops, date_out)
            if not out.strip():
                missing.append(base)
            return out
        if base in ("date", "mdate"):
            d = ctx.get(base) or datetime.now()
            return apply_ops(d.strftime(spec or date_out), ops, date_out)
        if base in ("original_name", "folder", "ext", "page", "first_line", "text_snippet"):
            return apply_ops(str(ctx.get(base, "")), ops, date_out)
        val = fields.get(base, "")
        out = apply_ops(val, ops, date_out)
        if not out.strip():
            missing.append(base)
        return out

    return TOKEN.sub(repl, pattern), missing


def fit_stem(dest_dir: str | Path, stem: str, ext: str, max_len: int = 150) -> str:
    stem = sanitize_component(stem, max_len) or "unnamed"
    room = MAX_FULL_PATH - len(str(dest_dir)) - len(ext) - 2
    if room < 8:
        room = 8
    if len(stem) > room:
        stem = stem[:room].rstrip(". ") or "unnamed"
    return stem


def unique_path(dest_dir: str | Path, stem: str, ext: str, policy: str = "suffix",
                taken: Optional[set] = None, exists: Callable[[str], bool] = os.path.exists,
                ask: Optional[Callable[[str], Optional[str]]] = None) -> Optional[Path]:
    """Pick a non-conflicting path. policy: suffix | skip | ask. Returns None to skip."""
    taken = taken if taken is not None else set()
    key = lambda p: str(p).lower() if os.name == "nt" else str(p)
    cand = Path(dest_dir) / f"{stem}{ext}"
    if key(cand) not in taken and not exists(str(cand)):
        taken.add(key(cand))
        return cand
    if policy == "skip":
        return None
    if policy == "ask" and ask:
        new = ask(str(cand))
        if not new:
            return None
        cand = Path(dest_dir) / sanitize_component(new)
        if key(cand) not in taken and not exists(str(cand)):
            taken.add(key(cand))
            return cand
    i = 1
    while True:
        cand = Path(dest_dir) / f"{stem}_{i}{ext}"
        if key(cand) not in taken and not exists(str(cand)):
            taken.add(key(cand))
            return cand
        i += 1
