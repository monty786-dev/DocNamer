"""The batch engine: scan -> read -> extract -> review -> name -> copy (+ marked image, text, report)."""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import shutil
import threading
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from . import naming
from .db import JobDB
from .env import lp
from .export import SearchablePdfWriter, words_json, write_text_outputs
from .extract import (PageCtx, extract_anchor, extract_zone, has_qr, is_blank, load_page, page_text,
                      page_words, pages_in, read_full, render_with_transform)
from .layout import words_to_text
from .library import Library
from .marker import draw_marks, libreoffice_preview, save_image, synthetic_docx_preview, synthetic_xlsx_preview
from .models import FieldValue, FileResult, ReviewItem
from .ocr import Engines
from .orientation import apply_transform, rotate_cw, rotate_skew
from .readers import (EncryptedError, UnsupportedError, kind_of, open_document, save_pdf_subset)
from .report import Report
from .review import Decision, apply_field_decision, build_item
from .settings import Settings, Template
from .templates import TemplateManager, layout_signature

log = logging.getLogger("docnamer.pipeline")
FIRST_STRATEGIES = ("first_match", "first_valid")


class Cancelled(Exception):
    pass


class RunControl:
    """Pause / resume / cancel for long runs (thread-safe)."""

    def __init__(self):
        self._go = threading.Event()
        self._go.set()
        self._cancel = threading.Event()

    def pause(self): self._go.clear()
    def resume(self): self._go.set()
    def cancel(self):
        self._cancel.set()
        self._go.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def checkpoint(self) -> None:
        self._go.wait()
        if self._cancel.is_set():
            raise Cancelled()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(lp(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tidy(stem: str) -> str:
    stem = re.sub(r"([ _\-.])\1+", r"\1", stem)
    stem = re.sub(r"[_\- ]{2,}", lambda m: m.group(0)[0], stem)
    return stem.strip(" _-.")


# =====================================================================================
class Pipeline:
    def __init__(self, settings: Settings, data_dir: Path | str,
                 review_cb: Optional[Callable[[ReviewItem], Optional[Decision]]] = None,
                 password_cb: Optional[Callable[[Path], Optional[str]]] = None,
                 conflict_cb: Optional[Callable[[str], Optional[str]]] = None,
                 log_cb: Optional[Callable[[str], None]] = None):
        self.st = settings
        self.data = Path(data_dir)
        self.tmp = self.data / "tmp"
        self.review_dir = self.data / "review"
        self.lib = Library(self.data / "library")
        self.engines = Engines(settings.ocr, self.data, library=self.lib)
        self.templates = TemplateManager(self.data / "templates")
        self.db = JobDB(self.data / "jobs.db")
        self.review_cb, self.password_cb, self.conflict_cb = review_cb, password_cb, conflict_cb
        self.log_cb = log_cb or (lambda m: None)
        self._taken: set = set()
        self._seen: dict = {}
        self._counters: dict = {}
        self._next_counter = settings.naming.counter_start
        self._overrides: dict = {}
        self.report: Optional[Report] = None

    # ---- helpers ----------------------------------------------------------------------
    def _log(self, msg: str, level=logging.INFO):
        log.log(level, msg)
        self.log_cb(msg)

    def out_dir(self) -> Path:
        return Path(self.st.output.output_dir or (Path(self.st.output.input_dir) / "DocNamer_output"))

    def list_files(self) -> list[Path]:
        o = self.st.output
        root = Path(o.input_dir)
        if not root.is_dir():
            raise FileNotFoundError(f"Input folder not found: {root}")
        out = self.out_dir().resolve()
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dp = Path(dirpath)
            depth = len(dp.relative_to(root).parts)
            if not o.recursive or (o.max_depth and depth >= o.max_depth):
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if (dp / d).resolve() != out and not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("~$") or fn.startswith("."):
                    continue
                low = fn.lower()
                rel = str((dp / fn).relative_to(root)).replace("\\", "/")
                if not any(fnmatch.fnmatch(low, pat.lower()) for pat in o.include):
                    continue
                if any(fnmatch.fnmatch(low, pat.lower()) or fnmatch.fnmatch(rel.lower(), pat.lower()) for pat in o.exclude):
                    continue
                if kind_of(dp / fn):
                    files.append(dp / fn)
        files.sort(key=lambda p: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(p))])
        return files

    def preview_image(self, path: Path | str, page: int = 0) -> Image.Image:
        """Upright (orientation-corrected) page image for drawing zones."""
        with open_document(path, self.password_cb, self.tmp / "conv") as doc:
            if not doc.visual:
                raise UnsupportedError("zones need a PDF or image; use anchors / cells for Word and Excel")
            ctx = load_page(doc, min(page, doc.page_count - 1), self.st, self.engines.build())
            return ctx.image

    def try_extract(self, path: Path | str, page: int = 0) -> dict:
        """Run the current zones/anchors on one page (live test inside the GUI)."""
        st = self.st
        with open_document(path, self.password_cb, self.tmp / "conv") as doc:
            ctx = load_page(doc, min(page, doc.page_count - 1), st, self.engines.build())
            out = {}
            for s in (st.zones if st.mode == "zones" else st.anchors):
                out[s.name] = (extract_zone if st.mode == "zones" else extract_anchor)(ctx, s, st, self.engines)
            return out

    # ---- analysis ------------------------------------------------------------------------
    def analyze(self, path: Path | str) -> list[FileResult]:
        path = Path(path)
        res = FileResult(path=str(path), kind=kind_of(path))
        root = Path(self.st.output.input_dir) if self.st.output.input_dir else path.parent
        try:
            res.rel = str(path.relative_to(root))
        except ValueError:
            res.rel = path.name
        try:
            res.sha = file_hash(path)
        except OSError as e:
            res.status, res.message = "error", f"cannot read file (locked / no permission?): {e}"
            return [res]
        try:
            doc = open_document(path, self.password_cb, self.tmp / "conv")
        except EncryptedError:
            res.status, res.message = "encrypted", "password-protected - skipped (no password given)"
            return [res]
        except UnsupportedError as e:
            res.status, res.message = "error", str(e)
            return [res]
        except Exception as e:
            log.exception("open failed: %s", path)
            res.status, res.message = "error", f"cannot open: {e}"
            return [res]
        try:
            st, pre = self._template_settings(doc, res)
            ov = self._overrides.get(str(path), {})
            segs = self._segments(doc, st, pre) if doc.kind == "pdf" and st.rules.split_on != "none" else None
            if segs and len(segs) > 1:
                outs = []
                for k, pages in enumerate(segs, 1):
                    seg_pdf = self.tmp / "segments" / f"{res.sha[:12]}_{k}.pdf"
                    save_pdf_subset(path, pages, seg_pdf)
                    r = FileResult(path=res.path, rel=res.rel, sha=f"{res.sha}-{k}", kind="pdf",
                                   source_file=str(seg_pdf), segment=k, seg_pages=[p + 1 for p in pages],
                                   template=res.template)
                    with open_document(seg_pdf) as d2:
                        self._analyze_doc(d2, r, st, ov)
                    outs.append(r)
                return outs
            res.source_file = str(path)
            self._analyze_doc(doc, res, st, ov, pre)
            return [res]
        except Cancelled:
            raise
        except Exception as e:
            log.exception("analysis failed: %s", path)
            res.status, res.message = "error", f"{type(e).__name__}: {e}"
            return [res]
        finally:
            doc.close()

    def _template_settings(self, doc, res: FileResult):
        st, pre = self.st, {}
        try:
            if st.template:
                t = self.templates.load(st.template)
                res.template = t.name
                return t.apply_to(st), pre
            if st.auto_template and self.templates.names():
                ctx0 = load_page(doc, 0, st, self.engines.build(), self._overrides.get(res.path, {}).get("pages"))
                pre[0] = ctx0
                text = page_text(ctx0, st, self.engines) if self.templates.needs_text() else ""
                t = self.templates.match(text, ctx0.image)
                if t:
                    res.template = t.name
                    return t.apply_to(st), pre
        except Exception as e:
            self._log(f"template selection failed: {e}", logging.WARNING)
        return st, pre

    def _segments(self, doc, st: Settings, pre: dict) -> list[list[int]]:
        """Split a multi-document PDF at separator pages (blank page or keyword)."""
        rules = st.rules
        segs, cur = [], []
        for i in range(doc.page_count):
            ctx = pre.get(i) or load_page(doc, i, st, self.engines.build())
            if rules.split_on == "blank":
                sep = is_blank(ctx, st)
            else:
                sep = bool(rules.split_keyword) and rules.split_keyword.lower() in page_text(ctx, st, self.engines).lower()
            if sep:
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append(i)
        if cur:
            segs.append(cur)
        return segs

    @staticmethod
    def _select_pages(rules, n: int) -> list[int]:
        p = (rules.pages or "first").strip().lower()
        if p == "first":
            return [0]
        if p == "last":
            return [n - 1]
        if p in ("all", "first_found"):
            return list(range(n))
        return [i for i in range(n) if pages_in(p, i)] or [0]

    @staticmethod
    def _passes_filters(fv: FieldValue, rules) -> bool:
        t = fv.text.lower()
        if any(m.lower() not in t for m in rules.must_contain):
            return False
        if any(m.lower() in t for m in rules.must_not_contain):
            return False
        return True

    def _choose(self, spec, cands: list[FieldValue], st: Settings) -> tuple[FieldValue, list[FieldValue]]:
        rules = st.rules
        floor = max(st.ocr.min_conf, rules.min_conf)
        ok = [c for c in cands if c.found and self._passes_filters(c, rules)]
        if not ok:
            fv = FieldValue(name=spec.name, required=spec.required, source="default" if spec.default else "")
            if spec.default:
                fv.text, fv.found, fv.conf, fv.valid = spec.default, True, 1.0, True
            else:
                fv.needs_review = spec.required
                if cands:
                    fv.page, fv.box, fv.crop_path = cands[0].page, cands[0].box, cands[0].crop_path
                    fv.candidates, fv.note = cands[0].candidates, cands[0].note
            return fv, []
        strat = rules.strategy
        if strat == "concat":
            first = ok[0]
            fv = FieldValue(**{**first.__dict__})
            fv.text = rules.concat_sep.join(c.text for c in ok)
            fv.conf = min(c.conf for c in ok)
            fv.valid = all(c.valid for c in ok)
            fv.needs_review = any(c.needs_review for c in ok)
            return fv, ok
        if strat == "highest_conf":
            best = max(ok, key=lambda c: (c.valid, c.conf))
        elif strat == "longest":
            best = max(ok, key=lambda c: len(c.text))
        elif strat == "first_match":
            best = ok[0]
        else:   # first_valid
            good = [c for c in ok if c.valid and c.conf >= floor]
            best = good[0] if good else max(ok, key=lambda c: (c.valid, c.conf))
        return best, [best]

    def _analyze_doc(self, doc, res: FileResult, st: Settings, ov: dict, pre: Optional[dict] = None) -> None:
        specs = st.zones if st.mode == "zones" else st.anchors if st.mode == "anchors" else []
        rules, n = st.rules, doc.page_count
        forced = ov.get("pages", {})
        tag = res.sha[:10]
        eng = self.engines.build()
        res.pattern, res.fallback = st.naming.pattern, st.naming.fallback
        res.engine = eng.printed.name if eng.printed else ("native-only" if not eng.errors else "none")
        sel = self._select_pages(rules, n)
        extras = [p for p in range(n) if p not in sel] if rules.on_missing == "next_page" else []
        cands: dict[str, list[FieldValue]] = {s.name: [] for s in specs}
        cache = dict(pre or {})
        floor = max(st.ocr.min_conf, rules.min_conf)
        orient_items: list[ReviewItem] = []

        def ctx_for(pi: int) -> PageCtx:
            ctx = cache.get(pi) or load_page(doc, pi, st, eng, forced)
            ctx.crop_dir, ctx.tag = self.review_dir, tag
            ctx.keep_crops = st.review.teach_handwriting
            cache.clear()                     # keep memory low: one page at a time
            return ctx

        def missing(s) -> bool:
            return not any(c.found for c in cands[s.name])

        def satisfied(s) -> bool:
            if rules.strategy == "first_match":
                return any(c.found and self._passes_filters(c, rules) for c in cands[s.name])
            if rules.strategy == "first_valid":
                return any(c.found and c.valid and c.conf >= floor and self._passes_filters(c, rules) for c in cands[s.name])
            return False

        scanned = 0
        for pi in sel + extras:
            is_extra = pi in extras
            if scanned >= max(1, rules.max_pages):
                break
            if specs:
                if is_extra and not any(missing(s) for s in specs):
                    break
                if not is_extra and rules.strategy in FIRST_STRATEGIES and all(satisfied(s) for s in specs):
                    break
            ctx = ctx_for(pi)
            res.page_transforms[pi] = dict(ctx.transform)
            if rules.skip_blank and is_blank(ctx, st):
                continue
            if rules.skip_keywords:
                txt = page_text(ctx, st, self.engines).lower()
                if any(k.lower() in txt for k in rules.skip_keywords):
                    continue
            if rules.skip_qr and has_qr(ctx):
                continue
            scanned += 1
            res.pages_read.append(pi)
            if ctx.orient and ctx.orient.uncertain and st.orientation.mode == "ask":
                it = self._orientation_item(res, ctx, pi, tag)
                if it:
                    if self.review_cb and st.review.mode == "interactive":
                        d = self.review_cb(it)
                        if d and d.text.isdigit():
                            forced[pi] = {"rot": int(d.text)}
                            ctx = load_page(doc, pi, st, eng, forced)
                            ctx.crop_dir, ctx.tag = self.review_dir, tag
                            res.page_transforms[pi] = dict(ctx.transform)
                    else:
                        orient_items.append(it)
            for s in specs:
                if rules.strategy in FIRST_STRATEGIES and satisfied(s) and not (is_extra and missing(s)):
                    continue
                if is_extra and not missing(s):
                    continue
                fn = extract_zone if st.mode == "zones" else extract_anchor
                fv = fn(ctx, s, st, self.engines)
                if fv.source == "n/a":
                    continue
                cands[s.name].append(fv)
            if st.mode == "full":
                break
        # ---- decide ---------------------------------------------------------------------
        for s in specs:
            fv, used = self._choose(s, cands[s.name], st)
            res.fields[s.name] = fv
            for u in used:
                if u.boxes or u.box or u.ref:
                    res.marks.append({"page": u.page, "field": s.name, "ref": u.ref,
                                      "boxes": u.boxes or [u.box], "chars": u.chars})
        if st.mode == "full" or st.output.save_text != "none" or st.output.searchable_pdf:
            self._export_full(doc, res, st, forced, tag)
        self._finalize_fields(res, st, orient_items)
        if res.status == "pending":
            res.status = "ok"
        res.confidence = (min((f.conf for f in res.fields.values() if f.found), default=1.0)
                          if res.fields else 1.0)

    def _orientation_item(self, res, ctx, pi, tag) -> Optional[ReviewItem]:
        try:
            self.review_dir.mkdir(parents=True, exist_ok=True)
            p = self.review_dir / f"{tag}_orient_p{pi}.jpg"
            th = ctx.image.copy()
            th.thumbnail((700, 700))
            th.save(p, quality=80)
            return ReviewItem(id=f"or{tag}{pi}", path=res.path, field="__orientation__", page=pi,
                              text=str(ctx.orient.guess), conf=ctx.orient.conf, crop_path=str(p),
                              candidates=[[str(a), 0.0, "rotate clockwise"] for a in (0, 90, 180, 270)],
                              reason="page orientation uncertain", kind="orientation")
        except Exception:
            return None

    def _finalize_fields(self, res: FileResult, st: Settings, orient_items: list) -> None:
        mode = st.review.mode
        for name, fv in res.fields.items():
            if not fv.needs_review:
                continue
            corr = self.lib.lookup_correction(fv.raw or fv.text) if (fv.raw or fv.text) else None
            if corr:
                fv.text, fv.conf, fv.valid, fv.found = corr, 0.99, True, True
                fv.needs_review, fv.note = False, "auto-corrected from library"
                continue
            if mode == "never":
                if fv.found and (fv.conf < st.ocr.min_conf or not fv.valid):
                    fv.text, fv.found = "", False
                fv.needs_review = False
                continue
            item = build_item(res.path, fv, self.lib, st)
            if mode == "interactive" and self.review_cb:
                d = self.review_cb(item)
                if d:
                    apply_field_decision(res, item, d, self.lib)
                    continue
            res.review.append(item)
        res.review.extend(orient_items)
        if st.review.teach_handwriting:                  # handwriting to learn from, asked after the run
            kinds = {s.name: s.kind for s in (st.zones if st.mode == "zones" else st.anchors)}
            for name, fv in res.fields.items():
                if kinds.get(name) == "handwritten" and fv.found and fv.crop_path and not fv.reviewed:
                    res.teach.append(ReviewItem(id=f"th{res.sha[:8]}{name}", path=res.path, field=name, page=max(fv.page, 0),
                                                text=fv.text, conf=fv.conf, crop_path=fv.crop_path,
                                                reason="handwriting sample", kind="teach"))
        if res.review:
            res.status = "review"
        if st.review.auto_harvest:
            self._harvest(res, st)

    def _harvest(self, res: FileResult, st: Settings) -> None:
        for fv in res.fields.values():
            if fv.found and fv.source not in ("native", "default") and fv.conf >= st.review.harvest_min_conf and fv.crop_path:
                try:
                    with Image.open(fv.crop_path) as im:
                        self.lib.add_sample(im.copy(), fv.text, "font", "auto", res.path, fv.page, fv.box, fv.conf)
                except Exception:
                    pass

    def _export_full(self, doc, res: FileResult, st: Settings, forced: dict, tag: str) -> None:
        eng = self.engines.build()
        o = st.output
        pages, writer = [], None
        if o.searchable_pdf and doc.visual:
            writer = SearchablePdfWriter(self.tmp / "text" / f"{tag}.pdf")
        total = doc.page_count if st.mode == "full" else min(doc.page_count, max(st.rules.max_pages, 1))
        for pi in range(total):
            ctx = load_page(doc, pi, st, eng, forced)
            text, ws = read_full(ctx, st, self.engines)
            pages.append({"page": pi + 1, "label": ctx.label, "text": text, "words": words_json(ws)})
            if writer is not None and ctx.image is not None:
                writer.add_page(ctx.image, ws, ctx.scale if ctx.has_native else 1.0 * (ctx.scale or 1.0))
        first = next((p["text"].strip() for p in pages if p["text"].strip()), "")
        first_line = first.splitlines()[0] if first else ""
        snippet = re.sub(r"\s+", " ", first)[:40]
        for nm, val in (("first_line", first_line), ("text_snippet", snippet)):
            res.fields[nm] = FieldValue(name=nm, text=val, conf=1.0, found=bool(val), required=False, source="text")
        tb = self.tmp / "text" / tag
        txt, js = write_text_outputs(pages, tb, o.save_text in ("txt", "both") or st.mode == "full",
                                     o.save_text in ("json", "both"))
        res.tmp_text, res.tmp_json = txt, js
        res.tmp_pdf = writer.save() if writer else ""

    # ---- naming & planning -----------------------------------------------------------------
    def name_for(self, res: FileResult, counter: int = 1) -> str:
        nc = self.st.naming
        fields = {k: v.text for k, v in res.fields.items()}
        src = Path(res.path)
        ext = src.suffix
        stem0 = src.stem + (f"_part{res.segment}" if res.segment > 0 else "")
        try:
            mdate = datetime.fromtimestamp(src.stat().st_mtime)
        except OSError:
            mdate = datetime.now()
        ctx = {"original_name": stem0, "folder": src.parent.name, "ext": ext.lstrip("."),
               "page": (res.pages_read[0] + 1) if res.pages_read else 1, "counter": counter, "mdate": mdate,
               "first_line": fields.get("first_line", ""), "text_snippet": fields.get("text_snippet", "")}
        pattern = res.pattern or nc.pattern
        fallback = res.fallback or nc.fallback
        stem, missing = naming.render_pattern(pattern, fields, ctx, nc.date_out)
        req_missing = [m for m in missing if (res.fields.get(m).required if m in res.fields else True)]
        if res.force_fallback or req_missing or not _tidy(stem):
            stem, _ = naming.render_pattern(fallback, fields, ctx, nc.date_out)
            if res.status == "ok":
                res.status = "fallback"
        return _tidy(stem) or stem0

    def _dest_dir(self, res: FileResult) -> Path:
        out = self.out_dir()
        rel_parent = Path(res.rel).parent
        if res.status in ("manual", "review"):
            return out / "needs_review" / rel_parent
        s = self.st.output.structure
        if s == "flat":
            return out / "renamed"
        if s.startswith("field:"):
            fv = res.fields.get(s[6:])
            return out / "renamed" / (naming.sanitize_component(fv.text) if fv and fv.text else "_unknown")
        return out / "renamed" / rel_parent

    @staticmethod
    def _key(p: Path) -> str:
        return str(p).lower() if os.name == "nt" else str(p)

    def plan(self, results: list[FileResult]) -> list[FileResult]:
        """Assign final destination folder + unique file name to every result (editable preview)."""
        dup = self.st.output.duplicates
        for r in results:
            if r.status in ("error", "encrypted", "skipped", "done"):
                continue
            base_sha = r.sha.split("-")[0] if r.segment < 0 else r.sha
            first = self._seen.get(base_sha)
            if first and first != (r.path, r.segment):
                r.duplicate_of = first[0]
                if dup == "skip":
                    r.status, r.message = "duplicate", f"identical to {Path(first[0]).name} - not copied"
                    continue
                if dup == "flag":
                    r.message = f"duplicate of {Path(first[0]).name}"
            else:
                self._seen[base_sha] = (r.path, r.segment)
            key = r.path + str(r.segment)
            if key not in self._counters:
                self._counters[key] = self._next_counter
                self._next_counter += 1
            r.counter = self._counters[key]
            ext = Path(r.path).suffix
            if r.new_name and r.dest_dir:                 # re-planning: free the earlier reservation
                self._taken.discard(self._key(Path(r.dest_dir) / r.new_name))
            d = self._dest_dir(r)
            if r.name_locked and r.new_name:
                stem = Path(r.new_name).stem
                ext = Path(r.new_name).suffix or ext
            else:
                stem = self.name_for(r, r.counter)
            stem = naming.fit_stem(d, stem, ext, self.st.naming.max_len)
            p = naming.unique_path(d, stem, ext, self.st.naming.conflict, self._taken, os.path.exists, self.conflict_cb)
            if p is None:
                r.status, r.message = "skipped", "name already exists (policy: skip)"
                continue
            r.dest_dir, r.new_name = str(p.parent), p.name
        return results

    # ---- review resolution ------------------------------------------------------------------
    def apply_review(self, res: FileResult, item: ReviewItem, dec: Decision,
                     all_results: Optional[list[FileResult]] = None) -> FileResult:
        if item.kind == "orientation":
            cw = int(dec.text or item.text or 0) % 360
            self._overrides.setdefault(res.path, {}).setdefault("pages", {})[item.page] = {"rot": cw}
            new = next((r for r in self.analyze(res.path) if r.segment == res.segment), None)
            if new:
                keep = (res.new_name, res.name_locked)
                res.__dict__.update(new.__dict__)
                res.new_name, res.name_locked = keep
            return res
        apply_field_decision(res, item, dec, self.lib, all_results)
        return res

    # ---- output -------------------------------------------------------------------------------
    def commit(self, res: FileResult) -> FileResult:
        if res.status in ("error", "encrypted", "skipped", "duplicate", "done") or not res.new_name:
            return res
        src = Path(res.source_file or res.path)
        dest = Path(res.dest_dir) / res.new_name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if os.path.exists(lp(dest)):      # lost a race / re-run: never overwrite
                p = naming.unique_path(dest.parent, dest.stem, dest.suffix, "suffix", set(), os.path.exists)
                dest = p
            shutil.copy2(lp(src), lp(dest))
            if os.path.getsize(lp(dest)) != os.path.getsize(lp(src)):
                raise OSError("size mismatch after copy")
            res.new_path = str(dest)
            out = self.out_dir()
            base = out / "needs_review" if self._is_under(dest.parent, out / "needs_review") else out / "renamed"
            rel_dir = dest.parent.relative_to(base) if self._is_under(dest.parent, base) else Path(".")
            if self.st.mark.enabled and res.marks:
                res.marked_paths = self.render_marked(res, out / "marked" / rel_dir, dest.stem)
            for tmp, ext, attr in ((res.tmp_text, ".txt", "text_path"), (res.tmp_json, ".json", "json_path"), (res.tmp_pdf, ".pdf", "pdf_path")):
                if tmp and Path(tmp).exists():
                    target = out / "text" / rel_dir / (dest.stem + (".searchable" if ext == ".pdf" else "") + ext)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(tmp, lp(target))
                    setattr(res, attr, str(target))
            if self.st.orientation.save_corrected_copy or self.st.output.save_corrected_copy:
                self._save_corrected(res, dest)
            res.status = "done"
            if self.report:
                self.report.add(res)
            self.db.put(res.sha, self.st.fingerprint(), res.path, "done", res.new_path, {"fields": res.field_text()})
        except Exception as e:
            log.exception("commit failed: %s", res.path)
            res.status, res.message = "error", f"copy failed: {e}"
        return res

    @staticmethod
    def _is_under(p: Path, parent: Path) -> bool:
        try:
            p.relative_to(parent)
            return True
        except ValueError:
            return False

    def render_marked(self, res: FileResult, folder: Path, stem: str) -> list[str]:
        cfg = self.st.mark
        paths, by_page = [], {}
        for m in res.marks:
            by_page.setdefault(m["page"], []).append(m)
        src = Path(res.source_file or res.path)
        with open_document(src, self.password_cb, self.tmp / "conv") as doc:
            for k, (pg, marks) in enumerate(sorted(by_page.items())):
                if doc.visual:
                    img = render_with_transform(doc, pg, self.st, res.page_transforms.get(pg, {}))
                    out = draw_marks(img, [{"boxes": m["boxes"], "label": m["field"], "chars": m["chars"]} for m in marks], cfg)
                else:
                    out = None
                    refs = {m["ref"] for m in marks if m.get("ref")}
                    labels = {m["ref"]: m["field"] for m in marks if m.get("ref")}
                    if cfg.use_libreoffice:
                        fv = res.fields.get(marks[0]["field"])
                        if fv and fv.text:
                            out = libreoffice_preview(src, [(marks[0]["field"], fv.text)], cfg, tmp=self.tmp / "lo")
                    if out is None:
                        words = doc.native_words(pg, 1.0)
                        out = (synthetic_docx_preview(words, refs, labels, cfg) if doc.kind == "docx"
                               else synthetic_xlsx_preview(words, doc.page_label(pg), refs, labels, cfg))
                name = f"{stem}_marked" + ("" if k == 0 else f"_p{pg + 1}")
                paths.append(str(save_image(out, folder / name, cfg.format)))
        return paths

    def _save_corrected(self, res: FileResult, dest: Path) -> None:
        """Optional extra copy with orientation fixed (images: pixels; PDFs: /Rotate, no deskew)."""
        out = dest.with_name(dest.stem + "_oriented" + dest.suffix)
        src = Path(res.source_file or res.path)
        if res.kind == "image":
            with open_document(src) as doc:
                img, _ = doc.render(0, self.st.ocr.dpi, 10**9)
                tf = res.page_transforms.get(0, {})
                apply_transform(img, tf.get("rot", 0), tf.get("skew", 0.0)).save(out)
        elif res.kind == "pdf":
            rots = {p: (t.get("pdf_rot", 0) + t.get("rot", 0)) % 360 for p, t in res.page_transforms.items()}
            with open_document(src) as doc:
                n = doc.page_count
            save_pdf_subset(src, list(range(n)), out, rots)

    def finish(self, results: list[FileResult], progress: Optional[Callable] = None) -> list[FileResult]:
        """After review: apply the 'unresolved' policy, re-plan and commit everything pending."""
        pol = self.st.review.unresolved
        if self.report is None:
            self.out_dir().mkdir(parents=True, exist_ok=True)
            self.report = Report(self.out_dir() / "report.csv")
        for r in results:
            if r.status == "review":
                if pol == "fallback":
                    r.force_fallback, r.status, r.review = True, "fallback", []
                elif pol == "guess":
                    r.status, r.review = "ok", []
                else:
                    r.status = "manual"
                    r.force_fallback = True
                    r.message = "unresolved fields - held in needs_review"
                    r.review = []
        self.plan([r for r in results if r.status in ("manual", "fallback", "ok")])
        n = 0
        todo = [r for r in results if r.status in ("ok", "fallback", "manual")]
        for r in todo:
            self.commit(r)
            n += 1
            if progress:
                progress(n, len(todo), f"saved {r.new_name}")
        return results

    # ---- run ---------------------------------------------------------------------------------------
    def run(self, dry_run: bool = False, control: Optional[RunControl] = None,
            progress: Optional[Callable[[int, int, str], None]] = None, reprocess: bool = False,
            commit_now: bool = True) -> list[FileResult]:
        st = self.st
        control = control or RunControl()
        files = self.list_files()
        self._taken, self._seen, self._counters = set(), {}, {}
        self._next_counter = st.naming.counter_start
        out = self.out_dir()
        if not dry_run:
            out.mkdir(parents=True, exist_ok=True)
            self.report = Report(out / "report.csv")
        total, results = len(files), []
        self._log(f"{total} file(s) found")
        fp = st.fingerprint()

        def handle(rs: list[FileResult], idx: int):
            for r in rs:
                results.append(r)
            ready = [r for r in rs if r.status in ("ok", "fallback")]
            self.plan(rs)
            if not dry_run and commit_now:
                for r in ready:
                    if r.status in ("ok", "fallback"):
                        self.commit(r)
            for r in rs:
                self._log(f"[{idx}/{total}] {Path(r.path).name} -> {r.new_name or '-'}  ({r.status}{': ' + r.message if r.message else ''})")

        to_do = []
        for f in files:
            if not reprocess:
                try:
                    sha = file_hash(f)
                    prev = self.db.done(sha, fp, str(f))
                except OSError:
                    prev = None
                if prev and Path(prev).exists():
                    r = FileResult(path=str(f), sha=sha, status="skipped", message=f"already processed -> {Path(prev).name}")
                    results.append(r)
                    continue
            to_do.append(f)
        done = total - len(to_do)
        try:
            if st.workers > 1 and len(to_do) > 1:
                if st.review.mode == "interactive":
                    self._log("interactive review is not available with several workers - switching to deferred")
                    st.review.mode = "deferred"
                with ProcessPoolExecutor(max_workers=st.workers, initializer=_winit,
                                         initargs=(st.to_dict(), str(self.data))) as ex:
                    for f, rd in zip(to_do, ex.map(_wanalyze, [str(f) for f in to_do], chunksize=1)):
                        control.checkpoint()
                        done += 1
                        handle([FileResult.from_dict(d) for d in rd], done)
                        if progress:
                            progress(done, total, f.name)
            else:
                for f in to_do:
                    control.checkpoint()
                    if progress:
                        progress(done, total, f"reading {f.name}")
                    handle(self.analyze(f), done + 1)
                    done += 1
                    if progress:
                        progress(done, total, f.name)
        except Cancelled:
            self._log("cancelled by user")
        return results

    # ---- plan persistence (dry-run -> edit -> apply) ------------------------------------------------
    def save_plan(self, results: list[FileResult], path: Path | str) -> None:
        import csv, json
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.with_suffix(".json")).write_text(json.dumps([r.to_dict() for r in results], ensure_ascii=False), encoding="utf-8")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["id", "original_path", "new_name", "status", "message"])
            for i, r in enumerate(results):
                w.writerow([i, r.path, r.new_name, r.status, r.message])

    def load_plan(self, path: Path | str) -> list[FileResult]:
        """Load a saved plan; names edited in the CSV 'new_name' column are locked in."""
        import csv, json
        path = Path(path)
        data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        results = [FileResult.from_dict(d) for d in data]
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                r = results[int(row["id"])]
                if row["new_name"] and row["new_name"] != r.new_name:
                    r.new_name, r.name_locked = row["new_name"], True
        return results

    def close(self):
        for c in (self.lib, self.db):
            try:
                c.close()
            except Exception:
                pass


# ---- multiprocessing workers (module level so they pickle on Windows 'spawn') ---------------------------
_WORKER: Optional[Pipeline] = None


def _winit(settings_dict: dict, data_dir: str) -> None:
    global _WORKER
    st = Settings.from_dict(settings_dict)
    st.workers = 1
    if st.review.mode == "interactive":
        st.review.mode = "deferred"
    _WORKER = Pipeline(st, data_dir)


def _wanalyze(path: str) -> list[dict]:
    return [r.to_dict() for r in _WORKER.analyze(path)]
