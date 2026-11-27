import random, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw, ImageFont

from docnamer.core import naming, textutil
from docnamer.core.env import find_tesseract
from docnamer.core.library import Library
from docnamer.core.ocr import Engines
from docnamer.core.ocr.library_engine import LibraryEngine, compose_letters
from docnamer.core.pipeline import Pipeline
from docnamer.core.review import Decision
from docnamer.core.settings import Anchor, OcrCfg, Settings, Zone
from docnamer.core.teach import TeachSession, analyze, auto_align

FREESANS = "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
HAVE_FONT = Path(FREESANS).exists()


def line_img(text, sz=46, seed=None, size=(760, 120)):
    rng = random.Random(seed)
    f = ImageFont.truetype(FREESANS, sz)
    im = Image.new("L", size, 255)
    ImageDraw.Draw(im).text((20, 20), text, fill=0, font=f)
    if seed is not None:
        im = im.rotate(rng.uniform(-2, 2), fillcolor=255, resample=Image.BICUBIC)
        s = rng.uniform(0.92, 1.08)
        im = im.resize((int(im.width * s), int(im.height * s)), Image.BILINEAR)
    return im.convert("RGB")


class TestHindiText(unittest.TestCase):
    def test_graphemes_and_script(self):
        self.assertEqual(textutil.graphemes("क्षत्रिय"), ["क्ष", "त्रि", "य"])
        self.assertEqual(textutil.script_of("रमेश"), "devanagari")
        self.assertEqual(textutil.script_of("Ramesh"), "latin")
        self.assertEqual(textutil.script_of("रमेश Kumar"), "mixed")

    def test_transliteration(self):
        self.assertEqual(textutil.transliterate("रमेश शर्मा"), "ramesh sharma")
        self.assertEqual(textutil.transliterate("दीपक जोशी"), "dipak joshi")
        self.assertEqual(textutil.transliterate("राजेश खन्ना"), "rajesh khanna")

    def test_hindi_dates_and_digits_in_patterns(self):
        stem, _ = naming.render_pattern("{n|translit}_{d|date:%Y%m%d}_{k|asciidigits}",
                                        {"n": "रमेश शर्मा", "d": "१२ मार्च २०२४", "k": "१२३४"}, {})
        self.assertEqual(stem, "ramesh sharma_20240312_1234")
        self.assertEqual(naming.parse_date("१२/०३/२०२४").strftime("%Y-%m-%d"), "2024-03-12")

    def test_letters_validator_accepts_matras(self):
        from docnamer.core.extract import validate
        z = Zone(name="n", fmt="letters")
        self.assertTrue(validate("रमेश शर्मा", z))        # the old \w regex rejected this
        self.assertFalse(validate("रमेश123", z))
        self.assertTrue(validate("१५,४००", Zone(name="n", fmt="number")))

    def test_unicode_filename_is_nfc_and_safe(self):
        self.assertEqual(naming.sanitize_component("रमेश शर्मा: बिल/२०२४"), "रमेश शर्मा_ बिल_२०२४")

    def test_compose_left_vowel_sign_order(self):
        self.assertEqual(compose_letters(["ि", "क"]), "कि")


class TestHindiEngineRouting(unittest.TestCase):
    def test_hindi_never_goes_to_rapidocr(self):
        e = Engines(OcrCfg(engine="auto", languages="hin"), "")
        self.assertTrue(e.needs_devanagari())
        with mock.patch.object(Engines, "_make", side_effect=lambda n: calls.append(n)) as m:
            calls = []
            e.build()
        self.assertEqual(calls, ["tesseract"])

    @unittest.skipUnless(find_tesseract(), "tesseract not installed")
    def test_missing_language_pack_gives_clear_error(self):
        with mock.patch("pytesseract.get_languages", return_value=["eng", "osd"]):
            e = Engines(OcrCfg(engine="tesseract", languages="eng+hin"), "").build()
        self.assertIsNone(e.printed)
        self.assertTrue(any("language data missing: hin" in m for m in e.errors), e.errors)


@unittest.skipUnless(HAVE_FONT, "needs FreeSans (Devanagari) font")
class TestHindiDocuments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.base = Path(cls.td.name)
        cls.inp = cls.base / "in"
        cls.inp.mkdir()
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
        pdfmetrics.registerFont(TTFont("FreeSans", FREESANS))
        c = canvas.Canvas(str(cls.inp / "hindi.pdf"))
        c.setFont("FreeSans", 14)
        c.drawString(60, 780, "नाम: रमेश शर्मा")
        c.drawString(60, 750, "दिनांक: १२ मार्च २०२४")
        c.save()
        import docx
        d = docx.Document()
        d.add_paragraph("नाम: दीपक जोशी")
        d.add_paragraph("दिनांक: ०५/०६/२०२४")
        d.save(cls.inp / "hindi.docx")

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def _run(self, translit=False):
        out = self.base / ("o_t" if translit else "o")
        st = Settings()
        st.mode = "anchors"
        st.anchors = [Anchor(name="name", keyword="नाम", fmt="letters"),
                      Anchor(name="dt", keyword="दिनांक", fmt="date")]
        st.naming.pattern = "{name|translit}_{dt|date:%Y%m%d}" if translit else "{name}_{dt|date:%Y%m%d}"
        st.output.input_dir, st.output.output_dir = str(self.inp), str(out)
        st.ocr.dpi = 150
        p = Pipeline(st, out / "data")
        return p.run(), out

    def test_hindi_pdf_and_docx_unicode_names(self):
        res, out = self._run()
        got = {Path(r.path).name: r.new_name for r in res}
        self.assertEqual(got["hindi.pdf"], "रमेश शर्मा_20240312.pdf")
        self.assertEqual(got["hindi.docx"], "दीपक जोशी_20240605.docx")
        self.assertTrue((out / "renamed" / "रमेश शर्मा_20240312.pdf").exists())

    def test_ascii_names_via_transliteration(self):
        res, _ = self._run(translit=True)
        self.assertEqual({Path(r.path).name: r.new_name for r in res},
                         {"hindi.pdf": "ramesh sharma_20240312.pdf", "hindi.docx": "dipak joshi_20240605.docx"})


@unittest.skipUnless(HAVE_FONT, "needs FreeSans (Devanagari) font")
class TestHandwritingTeaching(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.lib = Library(self.td.name)

    def tearDown(self):
        self.lib.close()
        self.td.cleanup()

    def _teach(self):
        for t in ("रमेश कुमार शर्मा", "दीपक जोशी", "Ramesh Kumar"):
            for k in range(3):
                r = auto_align(self.lib, line_img(t, seed=k), t, profile="Writer A")
                self.assertFalse(r["mismatch"])

    def test_auto_align_and_mismatch(self):
        self._teach()
        self.assertGreaterEqual(self.lib.count("word"), 15)
        self.assertGreater(self.lib.count("char"), 20)
        r = auto_align(self.lib, line_img("रमेश कुमार", seed=9), "रमेश", profile="Writer A")
        self.assertTrue(r["mismatch"])
        self.assertEqual((r["found_words"], r["typed_words"]), (2, 1))

    def test_taught_handwriting_is_recognised_with_confidence(self):
        self._teach()
        eng = LibraryEngine(self.lib)
        ws = eng.recognize(line_img("रमेश कुमार", seed=101))
        self.assertEqual([w.text for w in ws], ["रमेश", "कुमार"])
        self.assertGreater(ws[0].conf, 0.8)
        ws = eng.recognize(line_img("Kumar Ramesh", seed=102))
        self.assertEqual([w.text for w in ws], ["Kumar", "Ramesh"])

    def test_unknown_writing_is_not_guessed_confidently(self):
        self._teach()
        ws = LibraryEngine(self.lib).recognize(line_img("मुंबई", seed=5))
        self.assertTrue(all(w.conf < 0.5 for w in ws), [(w.text, w.conf) for w in ws])
        self.assertFalse(any("?" == w.text for w in ws))

    def test_session_asks_each_piece_and_letters(self):
        s = TeachSession(self.lib, line_img("पटेल सिंह"), profile="Writer B", hint_text="पटेल सिंह")
        self.assertEqual(s.total, 2)
        self.assertEqual(s.current().guess, "पटेल")               # pre-filled from what the user typed
        queued = s.ask_letters_next()
        self.assertGreaterEqual(queued, 2)
        s.save("पटेल")                                              # the word
        for _ in range(queued):                                     # then its letter pieces, each labelled by the user
            self.assertEqual(s.current().unit, "char")
            s.save("ट")
        self.assertEqual(s.current().unit, "word")
        s.skip()
        self.assertTrue(s.finished)
        self.assertEqual(self.lib.count("word"), 1)
        self.assertEqual(self.lib.count("char"), queued)

    def test_library_migration_from_old_schema(self):
        import sqlite3
        old = Path(self.td.name) / "old"
        (old / "samples").mkdir(parents=True)
        db = sqlite3.connect(old / "library.db")
        db.executescript("CREATE TABLE samples(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, label TEXT, image TEXT, source_doc TEXT,"
                         " page INTEGER, bbox TEXT, profile TEXT, conf_before REAL, verified INTEGER, created TEXT, emb BLOB);")
        line_img("रमेश").save(old / "samples" / "a.png")
        db.execute("INSERT INTO samples(kind,label,image,verified) VALUES('handwriting','रमेश','samples/a.png',1)")
        db.commit()
        db.close()
        lib = Library(old)
        row = lib.list_samples()[0]
        self.assertEqual((row["unit"], row["script"]), ("word", "devanagari"))
        self.assertEqual(lib.suggest(line_img("रमेश"), 1)[0][0], "रमेश")
        lib.close()


@unittest.skipUnless(HAVE_FONT and find_tesseract(), "needs FreeSans + tesseract")
class TestHandwritingInPipeline(unittest.TestCase):
    def test_handwritten_zone_read_from_taught_library_then_teach_flow(self):
        td = tempfile.TemporaryDirectory()
        base = Path(td.name)
        inp = base / "in"
        inp.mkdir()
        page = Image.new("RGB", (1240, 1754), "white")
        sample = line_img("रमेश कुमार", seed=77)
        page.paste(sample, (100, 300))
        page.save(inp / "form.png")                  # "handwritten" name written in the zone at y=300..420
        zone = Zone(name="name", x=0.05, y=0.15, w=0.7, h=0.12, kind="handwritten")
        out = base / "out"
        st = Settings()
        st.zones = [zone]
        st.naming.pattern, st.naming.fallback = "{name}", "UNREAD_{original_name}"
        st.output.input_dir, st.output.output_dir = str(inp), str(out)
        st.orientation.mode = "off"
        st.review.mode, st.review.teach_handwriting = "deferred", True
        p = Pipeline(st, out / "data")
        # 1) nothing taught yet: field is unreadable -> review queue (asks the user), not a wrong name
        res = p.run(commit_now=False)
        r = res[0]
        self.assertEqual(r.status, "review")
        item = next(i for i in r.review if i.field == "name")
        # 2) user types the text and ticks 'save to library' -> words aligned & learned automatically
        p.apply_review(r, item, Decision("edit", "रमेश कुमार", save_library=True, profile="Writer A"), res)
        self.assertEqual(p.lib.count("word"), 2)
        self.assertFalse(r.teach)                     # counts matched -> nothing left to ask
        p.finish(res)
        self.assertEqual(r.new_name, "रमेश कुमार.png")
        # 3) same handwriting (new jitter) in a new document: now read automatically via the library
        page2 = Image.new("RGB", (1240, 1754), "white")
        page2.paste(line_img("रमेश कुमार", seed=78), (100, 300))
        inp2 = base / "in2"
        inp2.mkdir()
        page2.save(inp2 / "form2.png")
        st2 = Settings.from_dict(st.to_dict())
        st2.output.input_dir, st2.output.output_dir = str(inp2), str(base / "out2")
        st2.review.teach_handwriting = True
        p2 = Pipeline(st2, base / "out" / "data")     # same data dir => same library
        res2 = p2.run(commit_now=False)
        r2 = res2[0]
        self.assertEqual(r2.fields["name"].text, "रमेश कुमार")
        self.assertEqual(r2.fields["name"].source, "library")
        self.assertEqual(r2.status, "ok")
        self.assertTrue(r2.teach, "teach_handwriting=True should offer this crop for confirmation")
        td.cleanup()


if __name__ == "__main__":
    unittest.main()
