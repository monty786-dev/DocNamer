import hashlib, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image

import make_samples
from docnamer.core.env import find_tesseract
from docnamer.core.pipeline import Pipeline
from docnamer.core.report import undo_report
from docnamer.core.review import Decision
from docnamer.core.settings import Anchor, Settings, Zone

HAS_TESS = find_tesseract() is not None


def tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha1(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def base_settings(inp: Path, out: Path) -> Settings:
    st = Settings()
    st.mode = "anchors"
    st.anchors = [Anchor(name="invoice", keyword="Invoice No", regex=r"[A-Z]{2}-?\d+"),
                  Anchor(name="date", keyword="Date", fmt="date", required=False)]
    st.naming.pattern = "{invoice}_{date|date:%Y%m%d|default:nodate}"
    st.naming.fallback = "UNREAD_{original_name}"
    st.output.input_dir, st.output.output_dir = str(inp), str(out)
    st.rules.pages, st.rules.strategy = "first_found", "first_valid"
    st.ocr.dpi = 150
    st.mark.enabled, st.mark.use_libreoffice = True, False
    return st


@unittest.skipUnless(HAS_TESS, "needs tesseract for the OCR paths")
class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.base = Path(cls.td.name)
        cls.inp = make_samples.build(cls.base / "in")

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def _run(self, name, mutate=None, **kw):
        out = self.base / name
        st = base_settings(self.inp, out / "out")
        if mutate:
            mutate(st)
        p = Pipeline(st, out / "data")
        return p, p.run(**kw), out / "out"

    def test_end_to_end_names_and_originals_untouched(self):
        before = tree_hash(self.inp)
        p, res, out = self._run("e2e")
        got = {Path(r.path).name: r.new_name for r in res if r.new_name}
        self.assertEqual(got["doc001.pdf"], "INV-1001_20240305.pdf")
        self.assertEqual(got["doc002.pdf"], "INV-1002_20240306.pdf")                # /Rotate 90 PDF
        self.assertEqual(got["scan_sideways.png"], "INV-2001_20240312.png")         # 90deg + skew scan
        self.assertEqual(got["scan_upside.jpg"], "INV-2002_20240313.jpg")           # 180deg scan
        self.assertEqual(got["scanned_two_page.pdf"], "INV-3001_20240420.pdf")      # blank cover skipped
        self.assertEqual(got["order.docx"], "DX-5001_20240509.docx")
        self.assertEqual(got["sheet.xlsx"], "XL-777_nodate.xlsx")
        self.assertEqual(got["doc001_copy.pdf"], "INV-1001_20240305_1.pdf")         # conflict -> suffix
        enc = next(r for r in res if r.path.endswith("secret.pdf"))
        self.assertEqual(enc.status, "encrypted")
        self.assertFalse(any(r.path.endswith("notes.txt") for r in res))
        self.assertEqual(tree_hash(self.inp), before, "originals were modified!")
        self.assertTrue((out / "renamed/client_b/scans/INV-2001_20240312.png").exists())   # structure mirrored
        self.assertEqual(len(list((out / "marked").rglob("*_marked.png"))), 8)   # incl. Word + Excel
        dup = next(r for r in res if r.path.endswith("doc001_copy.pdf"))
        self.assertIn("duplicate", dup.message)

    def test_duplicate_skip_policy(self):
        _, res, out = self._run("dup", lambda s: setattr(s.output, "duplicates", "skip"))
        dup = next(r for r in res if r.path.endswith("doc001_copy.pdf"))
        self.assertEqual(dup.status, "duplicate")
        self.assertFalse(list(out.rglob("*_1.pdf")))

    def test_resume_then_undo(self):
        p, res, out = self._run("resume")
        n_files = len(list((out / "renamed").rglob("*.*")))
        p2 = Pipeline(p.st, self.base / "resume" / "data")
        res2 = p2.run()
        self.assertTrue(all(r.status in ("skipped", "encrypted") for r in res2))
        self.assertEqual(len(list((out / "renamed").rglob("*.*"))), n_files)
        info = undo_report(out / "report.csv", out)
        self.assertGreaterEqual(info["removed"], n_files)
        self.assertEqual(info["refused"], 0)
        self.assertFalse(list((out / "renamed").rglob("*.*")))
        self.assertEqual(len(list(self.inp.rglob("*.*"))), 10)          # originals intact

    def test_dry_run_writes_nothing(self):
        _, res, out = self._run("dry", dry_run=True)
        self.assertFalse(out.exists() and any(out.rglob("*.pdf")))
        self.assertTrue(all(r.new_name for r in res if r.status != "encrypted"))

    def test_parallel_workers_match_serial(self):
        _, serial, _ = self._run("ser", dry_run=True)
        _, par, _ = self._run("par", lambda s: setattr(s, "workers", 2), dry_run=True)
        a = {Path(r.path).name: r.new_name for r in serial}
        b = {Path(r.path).name: r.new_name for r in par}
        self.assertEqual(a, b)

    def test_subfolder_toggle(self):
        _, res, _ = self._run("flat", lambda s: setattr(s.output, "recursive", False), dry_run=True)
        self.assertEqual({Path(r.path).name for r in res}, set())      # all files live in sub-folders

    def test_zone_mode_on_ocr_scan(self):
        def m(s):
            s.mode = "zones"
            s.zones = [Zone(name="inv", x=0.07, y=0.158, w=0.75, h=0.042, regex=r"INV-\d+"),
                       Zone(name="cust", x=0.07, y=0.254, w=0.75, h=0.042, fmt="letters", required=False)]
            s.naming.pattern = "{inv|re:(INV-\\d+)}"
            s.rules.pages = "first"
        _, res, _ = self._run("zones", m, dry_run=True)
        r = next(r for r in res if r.path.endswith("scan_sideways.png"))
        self.assertEqual(r.new_name, "INV-2001.png", {k: v.text for k, v in r.fields.items()})

    def test_split_multi_document_pdf(self):
        d = self.base / "split_in"
        d.mkdir()
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(d / "batch.pdf"), pagesize=A4)
        for txt in ("Invoice No: AA-111", None, "Invoice No: BB-222", None, "Invoice No: CC-333"):
            if txt:
                c.drawString(60, 780, txt)
            c.showPage()
        c.save()
        out = self.base / "split_out"
        st = base_settings(d, out)
        st.rules.split_on, st.rules.pages = "blank", "first"
        p = Pipeline(st, out / "data")
        res = p.run()
        self.assertEqual(sorted(r.new_name for r in res), ["AA-111_nodate.pdf", "BB-222_nodate.pdf", "CC-333_nodate.pdf"])
        self.assertEqual(sorted(r.seg_pages for r in res), [[1], [3], [5]])

    def _low_conf_settings(self, s):
        s.ocr.min_conf = 0.999999                      # force every OCR value into review
        s.output.input_dir = str(self.base / "rv_in")

    def test_review_deferred_flow_with_apply_similar(self):
        d = self.base / "rv_in"
        d.mkdir(exist_ok=True)
        for i, name in enumerate(("Asha Verma", "Asha Verma")):
            make_samples.invoice_image(f"INV-{500 + i}", "01/02/2024", name).save(d / f"s{i}.png")
        out = self.base / "rv_out"
        st = base_settings(d, out)
        st.ocr.min_conf = 0.999999
        st.review.mode = "deferred"
        p = Pipeline(st, out / "data")
        res = p.run()
        pending = [r for r in res if r.status == "review"]
        self.assertEqual(len(pending), 2)
        self.assertFalse((out / "renamed").exists(), "nothing may be committed while review is pending")
        r0 = pending[0]
        item = next(i for i in r0.review if i.field == "invoice")
        old_raw = r0.fields["invoice"].raw
        self.assertTrue(item.candidates and Path(item.crop_path).exists())
        p.apply_review(r0, item, Decision("edit", "INV-FIXED", apply_similar=True, save_library=True, profile="Writer A"),
                       all_results=res)
        for r in pending:                              # resolve the remaining items as 'accept'
            for it in list(r.review):
                p.apply_review(r, it, Decision("accept"), all_results=res)
        p.finish(res)
        names = sorted(Path(r.new_path).name for r in res)
        self.assertEqual(len(names), 2)
        self.assertTrue(any("INV-FIXED" in n for n in names), names)
        self.assertEqual(p.lib.profiles()[0]["name"], "Writer A")
        self.assertEqual(p.lib.lookup_correction(old_raw), "INV-FIXED")     # remembered for next time
        self.assertEqual(len(p.lib.list_samples()), 1)

    def test_review_never_uses_fallback(self):
        d = self.base / "nv_in"
        d.mkdir(exist_ok=True)
        make_samples.invoice_image("INV-9", "01/02/2024", "X Y").save(d / "a.png")
        out = self.base / "nv_out"
        st = base_settings(d, out)
        st.ocr.min_conf, st.review.mode = 0.999999, "never"
        res = Pipeline(st, out / "data").run()
        self.assertEqual(res[0].new_name, "UNREAD_a.png")
        self.assertEqual(res[0].status, "done")

    def test_review_interactive_callback_and_unresolved_folder(self):
        d = self.base / "ia_in"
        d.mkdir(exist_ok=True)
        make_samples.invoice_image("INV-9", "01/02/2024", "X Y").save(d / "a.png")
        out = self.base / "ia_out"
        st = base_settings(d, out)
        st.ocr.min_conf, st.review.mode = 0.999999, "interactive"
        seen = []
        def cb(item):
            seen.append(item.field)
            return Decision("edit", "ZZ-1") if item.field == "invoice" else Decision("skip")
        p = Pipeline(st, out / "data", review_cb=cb)
        res = p.run()
        self.assertIn("invoice", seen)
        self.assertEqual(res[0].new_name, "ZZ-1_nodate.png")
        # unresolved + deferred => held in needs_review with fallback name
        out2 = self.base / "ia_out2"
        st2 = base_settings(d, out2)
        st2.ocr.min_conf = 0.999999
        p2 = Pipeline(st2, out2 / "data")
        res2 = p2.run()
        p2.finish(res2)
        self.assertEqual(res2[0].status, "done")
        self.assertIn("needs_review", res2[0].new_path)

    def test_orientation_ask_mode_creates_review_item_and_fix(self):
        d = self.base / "or_in"
        d.mkdir(exist_ok=True)
        from docnamer.core.orientation import rotate_cw
        small = Image.new("RGB", (1240, 1754), "white")
        from PIL import ImageDraw
        ImageDraw.Draw(small).text((100, 100), "Invoice No: QQ-77", fill="black", font=make_samples._font(36))
        rotate_cw(small, 90).save(d / "tiny.png")      # needs 270 cw correction
        out = self.base / "or_out"
        st = base_settings(d, out)
        st.orientation.mode = "ask"
        st.orientation.threshold = 1.01                # unreachable => always "uncertain"
        p = Pipeline(st, out / "data")
        res = p.run()
        r = res[0]
        items = [i for i in r.review if i.kind == "orientation"]
        self.assertEqual(len(items), 1)
        p.apply_review(r, items[0], Decision("edit", items[0].candidates[3][0]))     # "270"
        self.assertEqual(r.fields["invoice"].text, "QQ-77")


if __name__ == "__main__":
    unittest.main()
