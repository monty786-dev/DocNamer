"""Geometry helpers: group words into lines, find phrases, pick words in a rectangle."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .models import Word


def box_union(boxes: Iterable) -> Optional[list]:
    bs = [b for b in boxes if b]
    if not bs:
        return None
    return [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]


def words_in_rect(words: list[Word], rect: tuple, min_overlap: float = 0.0) -> list[Word]:
    """Words whose centre lies inside rect = (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = rect
    out = []
    for w in words:
        if not w.box:
            continue
        if x0 <= w.cx <= x1 and y0 <= w.cy <= y1:
            out.append(w)
    return out


def group_lines(words: list[Word], tol: float = 0.55) -> list[list[Word]]:
    """Cluster words into text lines (top-to-bottom, left-to-right).

    Words without a box (docx paragraphs, xlsx cells) each form their own line,
    in the order given.
    """
    boxed = [w for w in words if w.box]
    unboxed = [w for w in words if not w.box]
    lines: list[list[Word]] = []
    if boxed:
        boxed = sorted(boxed, key=lambda w: w.cy)
        cur: list[Word] = []
        cur_cy = cur_h = 0.0
        for w in boxed:
            h = max(w.h, 1.0)
            if cur and abs(w.cy - cur_cy) <= tol * max(cur_h, h):
                cur.append(w)
                n = len(cur)
                cur_cy += (w.cy - cur_cy) / n
                cur_h = max(cur_h, h) if n < 3 else (cur_h * (n - 1) + h) / n
            else:
                if cur:
                    lines.append(sorted(cur, key=lambda a: a.box[0]))
                cur, cur_cy, cur_h = [w], w.cy, h
        if cur:
            lines.append(sorted(cur, key=lambda a: a.box[0]))
    lines.extend([w] for w in unboxed)
    return lines


def line_text(line: list[Word]) -> str:
    return " ".join(w.text for w in line if w.text)


def lines_to_text(lines: list[list[Word]], sep: str = "\n") -> str:
    return sep.join(line_text(l) for l in lines if l)


def words_to_text(words: list[Word], sep: str = "\n") -> str:
    return lines_to_text(group_lines(words), sep)


@dataclass
class Match:
    line: int
    first: int          # index of first word in the line
    last: int           # index of last word in the line
    start: int          # char span inside the joined line string
    end: int
    tail: str           # text of the last word after the match end (if partially covered)


def find_matches(lines: list[list[Word]], pattern: str, is_regex: bool = False,
                 case_sensitive: bool = False) -> list[Match]:
    flags = 0 if case_sensitive else re.IGNORECASE
    rx = re.compile(pattern if is_regex else re.escape(pattern).replace(r"\ ", r"\s+"), flags)
    out: list[Match] = []
    for li, line in enumerate(lines):
        spans = []
        pos = 0
        parts = []
        for w in line:
            spans.append((pos, pos + len(w.text)))
            parts.append(w.text)
            pos += len(w.text) + 1
        s = " ".join(parts)
        for m in rx.finditer(s):
            if m.end() == m.start():
                continue
            first = next((i for i, (a, b) in enumerate(spans) if b > m.start()), None)
            last = next((i for i in range(len(spans) - 1, -1, -1) if spans[i][0] < m.end()), None)
            if first is None or last is None:
                continue
            tail = s[m.end():spans[last][1]] if m.end() < spans[last][1] else ""
            out.append(Match(li, first, last, m.start(), m.end(), tail))
    return out


def sub_box(w: Word, frac_start: float, frac_end: float = 1.0) -> list:
    """Approximate box of a slice of a word (for partially matched units)."""
    x0, y0, x1, y1 = w.box
    width = x1 - x0
    return [x0 + width * frac_start, y0, x0 + width * frac_end, y1]
