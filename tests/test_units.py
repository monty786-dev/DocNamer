import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from PIL import Image

import make_samples
from docnamer.core import naming
from docnamer.core.env import find_tesseract
from docnamer.core.layout import find_matches, group_lines, line_text
from docnamer.core.models import Word
from docnamer.core.ocr.handwriting import ctc_greedy
from docnamer.core.orientation import detect_orientation, rotate_cw, rotate_skew
from docnamer.core.settings import Anchor, OrientationCfg, Settings, Template, Zone

HAS_TESS = find_tesseract() is not None


class TestNaming(unittest.TestCase):
    def test_tokens_and_ops(self):
        stem, miss = naming.render_pattern("{inv|upper|trim}_{dt|date:%Y%m%d}_{counter:03}_{x|default:NA}",
                                           {"inv": " ab-12 ", "dt": "5 March 2024", "x": ""}, {"counter": 7})
        self.assertEqual(stem, "AB-12_20240305_007_NA")
        self.assertEqual(miss, [])

    def test_user_field_beats_builtin_date(self):
        stem, _ = naming.render_pattern("{date|date:%Y}", {"date": "01/02/2020"}, {})
        self.assertEqual(stem, "2020")

    def test_missing_reported(self):
        _, miss = naming.render_pattern("{a}_{b}", {"a": "x"}, {})
        self.assertEqual(miss, ["b"])

    def test_windows_safety(self):
        self.assertEqual(naming.sanitize_component("CON"), "_CON")
        self.assertEqual(naming.sanitize_component('a<b>:c?. '), "a_b__c_")
        self.assertLessEqual(len(naming.fit_stem("C:/" + "x" * 200, "y" * 300, ".pdf")) + 205, 245)

    def test_unique_paths(self):
        with tempfile.TemporaryDirectory() as td:
            taken = set()
            a = naming.unique_path(td, "n", ".pdf", "suffix", taken)
            b = naming.unique_path(td, "n", ".pdf", "suffix", taken)
            c = naming.unique_path(td, "n", ".pdf", "skip", taken)
            self.assertEqual((a.name, b.name, c), ("n.pdf", "n_1.pdf", None))


class TestLayoutAndCtc(unittest.TestCase):
    def test_phrase_match_across_words(self):
        ws = [Word("Invoice", [10, 10, 60, 30]), Word("No:", [65, 10, 90, 30]), Word("123", [100, 11, 150, 31])]
        lines = group_lines(ws)
        self.assertEqual(line_text(lines[0]), "Invoice No: 123")
        m = find_matches(lines, "invoice no")[0]
        self.assertEqual((m.first, m.last), (0, 1))

    def test_ctc_repeat_needs_blank(self):
        def dec(seq):
            lg = np.full((len(seq), 4), -5.0)
            for t, k in enumerate(seq):
                lg[t, k] = 5.0
            return ctc_greedy(lg, list("abc"), 0)[0]
        self.assertEqual(dec([0, 1, 1, 0, 2, 2, 0, 3]), "abc")
        self.assertEqual(dec([1, 0, 1]), "aa")


@unittest.skipUnless(HAS_TESS, "tesseract not installed")
class TestOrientation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        img = make_samples.invoice_image("INV-1", "01/01/2024", "Test Person")
        # repeat text so OSD has plenty to look at
        cls.img = img

    def test_four_rotations(self):
        cfg = OrientationCfg()
        for need in (0, 90, 180, 270):
            bad = rotate_cw(self.img, (360 - need) % 360)
            r = detect_orientation(bad, cfg, None)
            self.assertEqual(r.cw, need, f"need {need} got {r}")

    def test_skew(self):
        r = detect_orientation(rotate_skew(self.img, 3.0), OrientationCfg(), None)
        self.assertAlmostEqual(r.skew, -3.0, delta=0.6)

    def test_ask_mode_does_not_rotate_when_unsure(self):
        faint = Image.new("RGB", (1240, 1754), "white")
        r = detect_orientation(faint, OrientationCfg(mode="ask"), None)
        self.assertEqual(r.cw, 0)

    def test_off(self):
        r = detect_orientation(rotate_cw(self.img, 90), OrientationCfg(mode="off"), None)
        self.assertEqual((r.cw, r.skew), (0, 0.0))


class TestReaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.root = make_samples.build(Path(cls.td.name) / "in")

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_pdf_words_and_rotation(self):
        from docnamer.core.readers import PdfDocument
        for f in ("doc001.pdf", "doc002.pdf"):          # doc002 has /Rotate 90
            d = PdfDocument(self.root / "client_a" / f)
            cw = d.native_orientation(0)
            img, s = d.render(0, 120, 10**8, rot=cw)
            ws = d.native_words(0, s, rot=cw)
            from docnamer.core.layout import words_to_text
            self.assertIn("Invoice No: INV-100", words_to_text(ws))
            g = np.asarray(img.convert("L"))
            for w in ws:
                x0, y0, x1, y1 = (int(v) for v in w.box)
                self.assertGreater((g[y0:y1, x0:x1] < 128).mean(), 0.03, w.text)

    def test_encrypted_pdf(self):
        from docnamer.core.readers import EncryptedError, PdfDocument
        with self.assertRaises(EncryptedError):
            PdfDocument(self.root / "client_b" / "secret.pdf")
        d = PdfDocument(self.root / "client_b" / "secret.pdf", lambda p: "s3cret")
        self.assertEqual(d.page_count, 1)

    def test_docx_table_cells_not_lost(self):
        from docnamer.core.readers import open_document
        ws = open_document(self.root / "client_a" / "order.docx").native_words(0, 1)
        self.assertEqual([w.text for w in ws][-2:], ["Date", "2024-05-09"])

    def test_xlsx_cell_addresses(self):
        from docnamer.core.readers import open_document
        ws = open_document(self.root / "client_a" / "sheet.xlsx").native_words(0, 1)
        self.assertIn("Summary!B1", [w.ref for w in ws])

    def test_csv(self):
        from docnamer.core.readers import open_document
        p = Path(self.td.name) / "t.csv"
        p.write_text("Name;Ref\nAsha;R-77\n", encoding="utf-8")
        ws = open_document(p).native_words(0, 1)
        self.assertIn("R-77", [w.text for w in ws])


class TestSettings(unittest.TestCase):
    def test_roundtrip_and_template(self):
        import json
        s = Settings()
        s.zones = [Zone(name="inv", x=.5, y=.1, w=.2, h=.05, kind="handwritten")]
        s.anchors = [Anchor(name="d", keyword="Date")]
        s2 = Settings.from_dict(json.loads(json.dumps(s.to_dict())))
        self.assertEqual(s.fingerprint(), s2.fingerprint())
        self.assertIsInstance(s2.zones[0], Zone)
        t = Template(name="t", zones=s.zones, pattern="{inv}")
        s3 = t.apply_to(Settings())
        self.assertEqual((s3.naming.pattern, s3.zones[0].name), ("{inv}", "inv"))

    def test_lite_defaults(self):
        s = Settings()
        s.apply_lite_defaults()
        self.assertTrue(s.lite)
        self.assertEqual(s.workers, 1)
        self.assertLessEqual(s.ocr.max_pixels, 12_000_000)


if __name__ == "__main__":
    unittest.main()
