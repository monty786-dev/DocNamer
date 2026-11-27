"""DocNamer command line:  docnamer run | apply | undo | library | template | info"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
from pathlib import Path

from .. import __version__
from ..core.env import data_dir as get_data_dir, find_soffice, find_tesseract, is_wine
from ..core.logsetup import setup_logging
from ..core.models import FileResult
from ..core.pipeline import Pipeline
from ..core.report import undo_report
from ..core.review import Decision
from ..core.settings import Anchor, Settings, Template, Zone


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="docnamer", description="Offline document reader & renamer")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--data-dir", help="portable data folder (settings, models, library, logs)")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="scan a folder and create renamed copies")
    r.add_argument("input", nargs="?", help="input folder")
    r.add_argument("-o", "--output", help="output folder")
    r.add_argument("--settings", help="settings JSON (saved by the GUI)")
    r.add_argument("--template", help="template name or .json path")
    r.add_argument("--mode", choices=["zones", "anchors", "full"])
    r.add_argument("--zone", action="append", default=[],
                   help='name=x,y,w,h[,kind[,fmt]]  (fractions 0-1 of the upright page; kind printed|handwritten|auto)')
    r.add_argument("--anchor", action="append", default=[],
                   help='name=keyword[|right|below|offset[|kind[|fmt]]]')
    r.add_argument("--pattern", help='name pattern, e.g. "{invoice}_{date|date:%%Y%%m%%d}"')
    r.add_argument("--fallback", help="pattern used when a required field is missing")
    r.add_argument("--no-recursive", action="store_true", help="do not enter sub-folders")
    r.add_argument("--orientation", choices=["auto", "ask", "off"])
    r.add_argument("--pages", help="first | last | all | first_found | 1,3-5")
    r.add_argument("--strategy", choices=["first_match", "first_valid", "highest_conf", "longest", "concat"])
    r.add_argument("--split", choices=["none", "blank", "keyword"])
    r.add_argument("--review", choices=["deferred", "interactive", "never"])
    r.add_argument("--min-conf", type=float)
    r.add_argument("--mark", choices=["rectangle", "ellipse", "underline", "highlight"], help="save marked images")
    r.add_argument("--save-text", choices=["txt", "json", "both"])
    r.add_argument("--searchable-pdf", action="store_true")
    r.add_argument("--duplicates", choices=["flag", "skip", "keep"])
    r.add_argument("--engine", choices=["auto", "rapid", "tesseract"])
    r.add_argument("--lang", help="tesseract languages, e.g. eng+hin")
    r.add_argument("--workers", type=int)
    r.add_argument("--lite", action="store_true", help="low-memory defaults (Winlator)")
    r.add_argument("--dry-run", action="store_true", help="analyse only; write a plan you can edit")
    r.add_argument("--plan", help="where to save the dry-run plan (CSV)")
    r.add_argument("--reprocess", action="store_true", help="ignore the 'already processed' memory")
    r.add_argument("--save-template", help="save the zones/anchors/naming as a template with this name")
    r.add_argument("-q", "--quiet", action="store_true")

    a = sub.add_parser("apply", help="commit a (possibly edited) dry-run plan")
    a.add_argument("plan")
    a.add_argument("--settings")
    a.add_argument("-o", "--output")
    a.add_argument("-q", "--quiet", action="store_true")

    u = sub.add_parser("undo", help="delete everything a previous run created (originals untouched)")
    u.add_argument("output", help="the output folder used by that run")

    l = sub.add_parser("library", help="handwriting / font library")
    l.add_argument("action", choices=["list", "profiles", "export-zip", "import-zip", "export-dataset"])
    l.add_argument("path", nargs="?")
    l.add_argument("--format", choices=["paddle", "trocr"], default="paddle")

    h = sub.add_parser("teach", help="teach handwriting: it asks which shabd (word) / akshar (letter) each piece is")
    h.add_argument("image", help="image or PDF containing the handwriting")
    h.add_argument("--page", type=int, default=1)
    h.add_argument("--zone", help="x,y,w,h as fractions (0-1) of the page, to teach only part of it")
    h.add_argument("--text", help='what it says, e.g. "रमेश कुमार" - aligned to the words found, no questions asked')
    h.add_argument("--profile", default="default", help="writer / font profile name")
    h.add_argument("--kind", choices=["handwriting", "font"], default="handwriting")
    h.add_argument("--mode", choices=["words", "letters", "both"], default="words")

    t = sub.add_parser("template", help="list saved templates")
    t.add_argument("action", choices=["list", "show", "delete"])
    t.add_argument("name", nargs="?")

    sub.add_parser("info", help="show environment (Wine / OCR engines / LibreOffice)")
    return p


def _parse_zone(s: str) -> Zone:
    name, _, rest = s.partition("=")
    v = [x.strip() for x in rest.split(",")]
    z = Zone(name=name.strip(), x=float(v[0]), y=float(v[1]), w=float(v[2]), h=float(v[3]))
    if len(v) > 4:
        z.kind = v[4]
    if len(v) > 5:
        z.fmt = v[5]
    return z


def _parse_anchor(s: str) -> Anchor:
    name, _, rest = s.partition("=")
    v = rest.split("|")
    a = Anchor(name=name.strip(), keyword=v[0])
    if len(v) > 1 and v[1]:
        a.direction = v[1]
    if len(v) > 2 and v[2]:
        a.kind = v[2]
    if len(v) > 3 and v[3]:
        a.fmt = v[3]
    return a


def settings_from_args(args, data: Path) -> Settings:
    st = Settings.load(args.settings) if getattr(args, "settings", None) else Settings.load(data / "settings.json")
    o = st.output
    if getattr(args, "input", None):
        o.input_dir = args.input
    if getattr(args, "output", None):
        o.output_dir = args.output
    if getattr(args, "no_recursive", False):
        o.recursive = False
    if getattr(args, "mode", None):
        st.mode = args.mode
    if getattr(args, "zone", None):
        st.zones = [_parse_zone(z) for z in args.zone]
        st.mode = args.mode or "zones"
    if getattr(args, "anchor", None):
        st.anchors = [_parse_anchor(a) for a in args.anchor]
        st.mode = args.mode or "anchors"
    if getattr(args, "template", None):
        st.template = Path(args.template).stem
    for attr, tgt in (("pattern", (st.naming, "pattern")), ("fallback", (st.naming, "fallback")),
                      ("orientation", (st.orientation, "mode")), ("pages", (st.rules, "pages")),
                      ("strategy", (st.rules, "strategy")), ("split", (st.rules, "split_on")),
                      ("review", (st.review, "mode")), ("min_conf", (st.ocr, "min_conf")),
                      ("duplicates", (o, "duplicates")), ("engine", (st.ocr, "engine")),
                      ("lang", (st.ocr, "languages")), ("workers", (st, "workers"))):
        v = getattr(args, attr, None)
        if v is not None:
            setattr(tgt[0], tgt[1], v)
    if getattr(args, "mark", None):
        st.mark.enabled, st.mark.style = True, args.mark
    if getattr(args, "save_text", None):
        o.save_text = args.save_text
    if getattr(args, "searchable_pdf", False):
        o.searchable_pdf = True
    if getattr(args, "lite", False) or is_wine():
        st.apply_lite_defaults()
    return st


def terminal_review(p: Pipeline, results: list[FileResult]) -> None:
    """Answer the deferred 'can't read this' queue in the terminal."""
    pending = [(r, it) for r in results if r.status == "review" for it in list(r.review)]
    if not pending:
        return
    print(f"\n{len(pending)} item(s) need your help.  [number]=choose candidate  e=type text  a=accept  "
          "s=skip field  f=fallback name  m=manual  q=stop reviewing")
    for n, (r, it) in enumerate(pending, 1):
        print(f"\n({n}/{len(pending)}) {Path(r.path).name}  page {it.page + 1}  field '{it.field}'  [{it.reason}]")
        if it.crop_path:
            print(f"   image: {it.crop_path}")
        print(f"   read as: {it.text!r}  (confidence {it.conf:.2f})")
        for i, c in enumerate(it.candidates, 1):
            print(f"   {i}. {c[0]!r}  ({c[2]} {c[1]:.2f})")
        while True:
            ans = input("   > ").strip()
            if ans.lower() == "q":
                return
            if ans.isdigit() and 1 <= int(ans) <= len(it.candidates):
                d = Decision("edit", it.candidates[int(ans) - 1][0] if it.kind == "field" else it.candidates[int(ans) - 1][0])
            elif ans.lower() == "e":
                d = Decision("edit", input("   text: ").strip())
            elif ans.lower() in ("a", ""):
                d = Decision("accept")
            elif ans.lower() == "s":
                d = Decision("skip")
            elif ans.lower() == "f":
                d = Decision("fallback")
            elif ans.lower() == "m":
                d = Decision("manual")
            else:
                continue
            if d.action == "edit" and it.kind == "field":
                d.apply_similar = input("   apply to all similar? [y/N] ").lower() == "y"
                d.save_library = input("   save to library? [y/N] ").lower() == "y"
            p.apply_review(r, it, d, results)
            break


def cmd_run(args, data: Path) -> int:
    st = settings_from_args(args, data)
    if not st.output.input_dir:
        print("error: give an input folder", file=sys.stderr)
        return 2
    if args.save_template:
        Pipeline(st, data).templates.save(Template(name=args.save_template, mode=st.mode, zones=st.zones,
                                                   anchors=st.anchors, pattern=st.naming.pattern,
                                                   fallback=st.naming.fallback, rules=st.rules.__dict__))
        print(f"template '{args.save_template}' saved")
    interactive = st.review.mode == "interactive"

    def ask(item):
        print(f"\n[{Path(item.path).name}] field '{item.field}' read as {item.text!r} ({item.reason}); image: {item.crop_path}")
        for i, c in enumerate(item.candidates, 1):
            print(f"   {i}. {c[0]!r}")
        a = input("   number / e=type / a=accept / s=skip / f=fallback > ").strip().lower()
        if a.isdigit() and 1 <= int(a) <= len(item.candidates):
            return Decision("edit", item.candidates[int(a) - 1][0])
        if a == "e":
            return Decision("edit", input("   text: ").strip())
        return {"s": Decision("skip"), "f": Decision("fallback")}.get(a, Decision("accept"))

    p = Pipeline(st, data, review_cb=ask if interactive else None,
                 password_cb=lambda path: input(f"password for {path.name} (blank = skip): ") or None if interactive else None,
                 log_cb=None if args.quiet else print)
    results = p.run(dry_run=args.dry_run, reprocess=args.reprocess, commit_now=not args.dry_run and st.review.mode != "deferred")
    if args.dry_run:
        plan = Path(args.plan or (p.out_dir() / "plan.csv"))
        p.save_plan(results, plan)
        print(f"\nplan saved: {plan}   (edit the new_name column, then: docnamer apply {plan})")
    else:
        if st.review.mode == "deferred":
            if sys.stdin.isatty():
                terminal_review(p, results)
            p.finish(results)
    _summary(results)
    return 0


def _summary(results: list[FileResult]) -> None:
    from collections import Counter
    c = Counter(r.status for r in results)
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(c.items())))


def cmd_apply(args, data: Path) -> int:
    st = settings_from_args(args, data)
    first = json.loads(Path(args.plan).with_suffix(".json").read_text(encoding="utf-8"))
    if not st.output.input_dir and first:
        st.output.input_dir = str(Path(first[0]["path"]).parent)
    p = Pipeline(st, data, log_cb=None if args.quiet else print)
    results = p.load_plan(args.plan)
    p.finish(results)
    _summary(results)
    return 0


def cmd_library(args, data: Path) -> int:
    from ..core.library import Library
    lib = Library(data / "library")
    if args.action == "list":
        for s in lib.list_samples(limit=200):
            print(f"{s['id']:5} {s['unit']:5} {s['kind']:12} {s['profile']:14} {s['label']}")
    elif args.action == "profiles":
        for pr in lib.profiles():
            print(f"{pr['name']:20} {pr['kind']:12} {pr['samples']} samples")
    elif args.action == "export-zip":
        lib.export_zip(args.path)
        print("exported", args.path)
    elif args.action == "import-zip":
        print("imported", lib.import_zip(args.path), "samples")
    elif args.action == "export-dataset":
        print("exported", lib.export_dataset(args.path, args.format), "samples to", args.path)
    return 0


def cmd_teach(args, data: Path) -> int:
    from PIL import Image
    from ..core.library import Library
    from ..core.teach import TeachSession, auto_align
    st = Settings.load(data / "settings.json")
    p = Pipeline(st, data)
    path = Path(args.image)
    img = p.preview_image(path, args.page - 1) if path.suffix.lower() == ".pdf" else Image.open(path).convert("RGB")
    if args.zone:
        x, y, w, h = (float(v) for v in args.zone.split(","))
        W, H = img.size
        img = img.crop((int(x * W), int(y * H), int((x + w) * W), int((y + h) * H)))
    lib = p.lib
    if args.text:
        r = auto_align(lib, img, args.text, args.kind, args.profile, str(path), args.page - 1)
        if r["mismatch"]:
            print(f"found {r['found_words']} word(s) in the image but you typed {r['typed_words']}; "
                  "run again without --text to label each piece")
            return 1
        print(f"saved {r['saved']} word(s) and {r['letters_saved']} letter(s) to profile '{args.profile}'")
        return 0
    sess = TeachSession(lib, img, str(path), args.page - 1, args.profile, args.kind, args.mode)
    out = data / "teach"
    out.mkdir(parents=True, exist_ok=True)
    print(f"{sess.total} piece(s). For each: open the image, type what it says (Hindi or English).\n"
          "  Enter = accept the guess   - = skip   l = also ask its letters   b = back   q = quit")
    while not sess.finished:
        seg = sess.current()
        f = out / f"piece_{sess.pos + 1}_{seg.unit}.png"
        seg.crop.save(f)
        guess = f" [{seg.guess}]" if seg.guess else ""
        kind = "shabd/word" if seg.unit == "word" else "akshar/letter"
        ans = input(f"({sess.pos + 1}/{sess.total}) {kind}  {f}{guess} > ").strip()
        if ans == "q":
            break
        if ans == "b":
            sess.back()
        elif ans == "-":
            sess.skip()
        elif ans == "l" and seg.unit == "word":
            print(f"   {sess.ask_letters_next()} letter piece(s) queued after this word")
        else:
            label = ans or seg.guess
            if not label:
                continue
            r = sess.save(label, also_letters=input("   also save its letters automatically? [y/N] ").lower() == "y" if seg.unit == "word" else False)
            if r["letter_mismatch"]:
                print(f"   letters found: {r['letter_mismatch'][0]}, typed: {r['letter_mismatch'][1]} - use 'l' to label them one by one")
    print(f"saved {len(sess.saved_ids)} sample(s)")
    return 0


def cmd_info(args, data: Path) -> int:
    from ..core.ocr import Engines
    from ..core.ocr.rapid_engine import RapidOcrEngine
    from ..core.settings import OcrCfg
    print(f"DocNamer {__version__}")
    print(f"  data dir         : {data}")
    print(f"  Wine/Winlator    : {is_wine()}")
    print(f"  RapidOCR         : {RapidOcrEngine.available()}")
    print(f"  Tesseract        : {find_tesseract() or 'not found'}")
    print(f"  LibreOffice      : {find_soffice() or 'not found (synthetic Word/Excel previews are used)'}")
    hw = data / "models" / "handwriting"
    print(f"  handwriting model: {'present' if (hw / 'rec.onnx').exists() else 'not installed (' + str(hw) + ')'}")
    return 0


def main(argv=None) -> int:
    multiprocessing.freeze_support()
    args = build_parser().parse_args(argv)
    data = get_data_dir(args.data_dir)
    setup_logging(data, console=False)
    if args.cmd == "run":
        return cmd_run(args, data)
    if args.cmd == "apply":
        return cmd_apply(args, data)
    if args.cmd == "undo":
        out = Path(args.output)
        rep = out / "report.csv"
        if not rep.exists():
            print("no report.csv in that folder")
            return 1
        print(undo_report(rep, out))
        return 0
    if args.cmd == "library":
        return cmd_library(args, data)
    if args.cmd == "template":
        from ..core.templates import TemplateManager
        tm = TemplateManager(data / "templates")
        if args.action == "list":
            print("\n".join(tm.names()) or "(no templates)")
        elif args.action == "show":
            print(json.dumps(tm.load(args.name).to_dict(), indent=2))
        else:
            tm.delete(args.name)
        return 0
    if args.cmd == "info":
        return cmd_info(args, data)
    if args.cmd == "teach":
        return cmd_teach(args, data)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
