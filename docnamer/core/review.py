"""'Can't read this' review queue: candidate building and applying user decisions."""
from __future__ import annotations

import difflib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image

from .library import Library
from .models import FieldValue, FileResult, ReviewItem
from .settings import Settings
from .teach import auto_align


@dataclass
class Decision:
    action: str = "accept"          # accept | edit | skip | fallback | manual
    text: str = ""
    apply_similar: bool = False
    save_library: bool = False
    kind: str = "handwriting"       # handwriting | font
    profile: str = "default"


def build_item(path: str, fv: FieldValue, lib: Optional[Library], st: Settings) -> ReviewItem:
    cands = [list(c) for c in fv.candidates]
    crop = None
    if fv.crop_path and Path(fv.crop_path).exists():
        try:
            with Image.open(fv.crop_path) as im:
                crop = im.copy()
        except Exception:
            crop = None
    if lib is not None:
        if crop is not None:
            cands = lib.rerank(crop, cands) if cands else cands
            cands += [[t, round(s, 3), "library"] for t, s in lib.suggest(crop, 3)]
        corr = lib.lookup_correction(fv.text) if fv.text else None
        if corr:
            cands.append([corr, 0.95, "correction"])
        if fv.text:
            snap = lib.spell_snap(fv.text)
            if snap != fv.text:
                cands.append([snap, 0.5, "vocabulary"])
    if fv.text and not any(c[0] == fv.text for c in cands):
        cands.insert(0, [fv.text, fv.conf, fv.source])
    seen, uniq = set(), []
    for c in cands:
        if c[0] and c[0] not in seen:
            seen.add(c[0])
            uniq.append(c)
    reason = "invalid format" if (fv.found and not fv.valid) else ("not found" if not fv.found else "low confidence")
    return ReviewItem(id=uuid.uuid4().hex[:10], path=path, field=fv.name, page=max(fv.page, 0), text=fv.text,
                      conf=fv.conf, crop_path=fv.crop_path, candidates=uniq[:5], reason=reason)


def apply_field_decision(res: FileResult, item: ReviewItem, dec: Decision, lib: Optional[Library],
                         all_results: Optional[list[FileResult]] = None) -> None:
    fv = res.fields.get(item.field)
    if fv is None:
        return
    old = fv.raw or fv.text
    if dec.action == "edit":
        fv.text, fv.conf, fv.valid, fv.found = dec.text.strip(), 1.0, True, bool(dec.text.strip())
        if lib is not None:
            for w in dec.text.split():
                lib.add_vocab(w)
            if dec.apply_similar:
                lib.add_correction(old, dec.text.strip())
                for other in all_results or []:
                    for it in list(other.review):
                        ofv = other.fields.get(it.field)
                        if it.kind == "field" and it.field == item.field and ofv and other is not res:
                            if difflib.SequenceMatcher(None, ofv.raw or ofv.text, old).ratio() >= 0.9:
                                ofv.text, ofv.conf, ofv.valid, ofv.found = dec.text.strip(), 0.99, True, True
                                ofv.needs_review, ofv.reviewed = False, True
                                other.review.remove(it)
                                if not other.review and other.status == "review":
                                    other.status = "ok"
    elif dec.action == "skip":
        fv.text, fv.found, fv.valid = "", False, False
    elif dec.action == "fallback":
        res.force_fallback = True
    elif dec.action == "manual":
        res.status = "manual"
        res.message = "marked: needs manual handling"
    fv.needs_review, fv.reviewed = False, True
    res.teach = [t for t in res.teach if t.field != item.field]       # user has now dealt with this crop
    if dec.save_library and lib is not None and fv.crop_path and Path(fv.crop_path).exists() and fv.text:
        with Image.open(fv.crop_path) as im:
            r = auto_align(lib, im.copy(), fv.text, dec.kind, dec.profile, res.path, fv.page)
        if r["mismatch"]:       # could not pair words automatically -> ask the user which shabd / akshar each piece is
            res.teach.append(ReviewItem(id=uuid.uuid4().hex[:10], path=res.path, field=item.field, page=fv.page,
                                        text=fv.text, conf=fv.conf, crop_path=fv.crop_path,
                                        reason=f"{r['found_words']} word(s) found in the image but {r['typed_words']} typed",
                                        kind="teach"))
    res.review = [r for r in res.review if r.id != item.id]
    if not res.review and res.status == "review":
        res.status = "ok"
