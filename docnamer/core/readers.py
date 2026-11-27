"""Document readers: PDF, DOCX/DOC/RTF, XLSX/XLS/CSV and images -> pages, native words, renders."""
from __future__ import annotations

import csv
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageOps

from .env import find_soffice, lp
from .models import Word
from .preprocess import cap_pixels

log = logging.getLogger("docnamer.readers")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif", ".heic", ".heif", ".jp2", ".j2k", ".jpx", ".ppm", ".pgm"}
DOCX_EXT = {".docx", ".docm", ".doc", ".rtf", ".odt"}
XLSX_EXT = {".xlsx", ".xlsm", ".xls", ".ods"}
CSV_EXT = {".csv", ".tsv"}


class EncryptedError(Exception):
    pass


class UnsupportedError(Exception):
    pass


def kind_of(path: Path) -> str:
    e = path.suffix.lower()
    if e == ".pdf":
        return "pdf"
    if e in IMAGE_EXT:
        return "image"
    if e in DOCX_EXT:
        return "docx"
    if e in XLSX_EXT:
        return "xlsx"
    if e in CSV_EXT:
        return "csv"
    return ""


class BaseDocument:
    kind = ""
    visual = False                # True when pages can be rendered to an image

    def __init__(self, path: Path):
        self.path = Path(path)
        self.page_count = 1

    def render(self, i: int, dpi: int, max_pixels: int, rot: int = 0) -> tuple[Image.Image, float]:
        raise UnsupportedError("not a visual document")

    def native_words(self, i: int, scale: float, rot: int = 0) -> list[Word]:
        return []

    def native_text_len(self, i: int) -> int:
        return sum(len(w.text) for w in self.native_words(i, 1.0))

    def native_orientation(self, i: int) -> int:
        return 0

    def page_label(self, i: int) -> str:
        return str(i + 1)

    def embedded_images(self, i: int = 0) -> list[Image.Image]:
        return []

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# =====================================================================================
class PdfDocument(BaseDocument):
    kind = "pdf"
    visual = True

    def __init__(self, path: Path, password_cb: Optional[Callable[[Path], Optional[str]]] = None):
        super().__init__(path)
        import pypdfium2 as pdfium
        self._pdfium = pdfium
        pwd = None
        for attempt in range(2):
            try:
                self.pdf = pdfium.PdfDocument(lp(path), password=pwd)
                break
            except pdfium.PdfiumError as e:
                if "password" in str(e).lower():
                    pwd = password_cb(path) if (password_cb and attempt == 0) else None
                    if not pwd:
                        raise EncryptedError(str(path))
                    continue
                raise
        self.page_count = len(self.pdf)
        self._pages: dict[int, object] = {}

    def _page(self, i: int):
        if i not in self._pages:
            if len(self._pages) > 3:           # keep memory low on big files
                for k in list(self._pages)[:2]:
                    try:
                        self._pages.pop(k).close()
                    except Exception:
                        pass
            self._pages[i] = self.pdf[i]
        return self._pages[i]

    def render(self, i: int, dpi: int, max_pixels: int, rot: int = 0) -> tuple[Image.Image, float]:
        """Render page i. `rot` = extra clockwise rotation (0/90/180/270) on top of the page's /Rotate."""
        page = self._page(i)
        w, h = page.get_size()
        scale = dpi / 72.0
        if w * h * scale * scale > max_pixels:
            scale = math.sqrt(max_pixels / (w * h))
        img = page.render(scale=scale, rotation=rot % 360).to_pil()
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img, scale

    def _geometry(self, page):
        prot = page.get_rotation() % 360
        try:
            ox, oy, ox1, oy1 = page.get_cropbox()
        except Exception:
            ox, oy, ox1, oy1 = page.get_mediabox()
        return prot, ox, oy, ox1 - ox, oy1 - oy

    def _char_boxes(self, page, limit: Optional[int], rot_extra: int, scale: float):
        """[(char, [x0,y0,x1,y1] in display pixels)] for non-space chars; None for gaps."""
        tp = page.get_textpage()
        n = tp.count_chars()
        if n == 0:
            return []
        text = tp.get_text_range()
        if len(text) != n:
            text = "".join(tp.get_text_range(k, 1) or "\x00" for k in range(n))
        prot, ox, oy, w0, h0 = self._geometry(page)
        rot = (prot + rot_extra) % 360

        def to_px(x, y):
            x -= ox
            y -= oy
            if rot == 0:
                return x * scale, (h0 - y) * scale
            if rot == 90:
                return y * scale, x * scale
            if rot == 180:
                return (w0 - x) * scale, y * scale
            return (h0 - y) * scale, (w0 - x) * scale

        out = []
        for k in range(n if limit is None else min(n, limit)):
            ch = text[k]
            if ch.isspace() or ch in ("\x00", "\ufffe"):
                out.append(None)
                continue
            l, b, r, t = tp.get_charbox(k)
            if r - l <= 0 and t - b <= 0:
                out.append(None)
                continue
            xa, ya = to_px(l, t)
            xb, yb = to_px(r, b)
            out.append((ch, [min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)]))
        return out

    def native_text_len(self, i: int) -> int:
        try:
            return len(re.sub(r"\s", "", self._page(i).get_textpage().get_text_range()))
        except Exception:
            return 0

    def native_orientation(self, i: int) -> int:
        """Clockwise rotation (0/90/180/270) that makes this page's TEXT LAYER read left-to-right.
        Handles /Rotate and landscape pages authored with rotated text. 0 when unsure/no text."""
        try:
            chars = self._char_boxes(self._page(i), 700, 0, 1.0)
        except Exception:
            return 0
        votes = {"r": 0, "l": 0, "d": 0, "u": 0}
        prev = None
        for c in chars:
            if c is None:
                prev = None
                continue
            if prev is not None:
                b1, b2 = prev[1], c[1]
                dx = (b2[0] + b2[2]) / 2 - (b1[0] + b1[2]) / 2
                dy = (b2[1] + b2[3]) / 2 - (b1[1] + b1[3]) / 2
                size = max(b1[2] - b1[0], b1[3] - b1[1], 1e-6)
                if abs(dx) < 4 * size and abs(dy) < 4 * size:
                    if abs(dx) >= abs(dy):
                        votes["r" if dx > 0 else "l"] += 1
                    else:
                        votes["d" if dy > 0 else "u"] += 1
            prev = c
        if sum(votes.values()) < 8:
            return 0
        best = max(votes, key=votes.get)
        return {"r": 0, "l": 180, "d": 270, "u": 90}[best]

    def native_words(self, i: int, scale: float, rot: int = 0) -> list[Word]:
        try:
            return self._words_pdfium(i, scale, rot)
        except Exception as e:
            log.debug("pdfium text failed on %s p%d (%s); trying pdfminer", self.path.name, i, e)
            try:
                return self._words_pdfminer(i, scale)
            except Exception as e2:
                log.debug("pdfminer failed: %s", e2)
                return []

    def _words_pdfium(self, i: int, scale: float, rot: int = 0) -> list[Word]:
        chars = self._char_boxes(self._page(i), None, rot, scale)
        words: list[Word] = []
        cur: list = []

        def flush():
            if not cur:
                return
            words.append(Word(text="".join(c[0] for c in cur),
                              box=[min(c[1][0] for c in cur), min(c[1][1] for c in cur),
                                   max(c[1][2] for c in cur), max(c[1][3] for c in cur)],
                              conf=1.0, source="native", chars=[[c[0], c[1], 1.0] for c in cur]))
            cur.clear()

        prev_box = None
        word_h = 0.0
        for c in chars:
            if c is None:
                flush()
                prev_box, word_h = None, 0.0
                continue
            box = c[1]
            if prev_box is not None:
                # reference height = tallest glyph so far (hyphens / dots have tiny boxes)
                hh = max(box[3] - box[1], word_h, 1.0)
                gap = box[0] - prev_box[2]
                dy = abs(max(box[1], prev_box[1]) - min(box[1], prev_box[1]))
                same_line = (min(box[3], prev_box[3]) - max(box[1], prev_box[1])) > -0.35 * hh
                if not same_line or gap > 0.8 * hh or gap < -2.5 * hh:
                    flush()
                    word_h = 0.0
            word_h = max(word_h, box[3] - box[1])
            cur.append(c)
            prev_box = box
        flush()
        return words

    def _words_pdfminer(self, i: int, scale: float) -> list[Word]:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTChar, LTTextContainer, LTTextLine
        words: list[Word] = []
        for pl in extract_pages(str(self.path), page_numbers=[i]):
            H = pl.height

            def walk(o):
                if isinstance(o, LTTextLine):
                    cur = []
                    for c in o:
                        if isinstance(c, LTChar) and not c.get_text().isspace():
                            cur.append(c)
                        else:
                            emit(cur)
                            cur = []
                    emit(cur)
                elif hasattr(o, "__iter__"):
                    for c in o:
                        walk(c)

            def emit(cur):
                if not cur:
                    return
                box = [min(c.x0 for c in cur) * scale, (H - max(c.y1 for c in cur)) * scale,
                       max(c.x1 for c in cur) * scale, (H - min(c.y0 for c in cur)) * scale]
                words.append(Word("".join(c.get_text() for c in cur), box, 1.0, "native"))

            walk(pl)
        return words

    def embedded_images(self, i: int = 0) -> list[Image.Image]:
        import pypdfium2.raw as raw
        out = []
        page = self._page(i)
        try:
            for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE]):
                try:
                    out.append(obj.get_bitmap().to_pil().convert("RGB"))
                except Exception:
                    continue
        except Exception as e:
            log.debug("embedded image extraction failed: %s", e)
        return out

    def close(self):
        for p in self._pages.values():
            try:
                p.close()
            except Exception:
                pass
        self._pages.clear()
        try:
            self.pdf.close()
        except Exception:
            pass


def save_pdf_subset(src: Path, pages: list[int], out: Path, rotations: Optional[dict] = None) -> None:
    """Write a new PDF with the given 0-based pages; optional extra clockwise rotations per source page."""
    import pypdfium2 as pdfium
    s = pdfium.PdfDocument(lp(src))
    d = pdfium.PdfDocument.new()
    d.import_pages(s, pages=pages)
    if rotations:
        for new_i, old_i in enumerate(pages):
            r = rotations.get(old_i, 0) % 360
            if r:
                pg = d[new_i]
                pg.set_rotation((pg.get_rotation() + r) % 360)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    d.save(lp(out))
    d.close()
    s.close()


# =====================================================================================
class ImageDocument(BaseDocument):
    kind = "image"
    visual = True

    def __init__(self, path: Path):
        super().__init__(path)
        if path.suffix.lower() in (".heic", ".heif"):
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                raise UnsupportedError("HEIC needs pillow-heif (pip install pillow-heif)")
        Image.MAX_IMAGE_PIXELS = None
        self.im = Image.open(lp(path))
        self.page_count = max(1, getattr(self.im, "n_frames", 1))

    def render(self, i: int, dpi: int, max_pixels: int, rot: int = 0) -> tuple[Image.Image, float]:
        if self.page_count > 1:
            self.im.seek(i)
        img = self.im.copy()
        img = ImageOps.exif_transpose(img) or img
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, "white")
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        img = cap_pixels(img, max_pixels)
        return img, img.size[0] / float(w)

    def close(self):
        try:
            self.im.close()
        except Exception:
            pass


# =====================================================================================
def convert_with_soffice(path: Path, fmt: str, outdir: Path, timeout: int = 180) -> Optional[Path]:
    exe = find_soffice()
    if not exe:
        return None
    outdir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="lo_profile_"))
    try:
        subprocess.run([exe, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
                        "--convert-to", fmt, "--outdir", str(outdir), str(path)],
                       capture_output=True, timeout=timeout, check=False)
    except Exception as e:
        log.warning("soffice failed: %s", e)
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    out = outdir / (path.stem + "." + fmt.split(":")[0])
    return out if out.exists() else None


class DocxDocument(BaseDocument):
    kind = "docx"

    def __init__(self, path: Path, tmp: Optional[Path] = None):
        super().__init__(path)
        self._tmp = tmp or Path(tempfile.gettempdir()) / "docnamer_conv"
        self.units: list[Word] = []
        self.images: list[bytes] = []
        self._load()

    def _load(self):
        p = self.path
        if p.suffix.lower() in (".doc", ".rtf", ".odt"):
            conv = convert_with_soffice(p, "docx", self._tmp)
            if conv is None:
                self.units = self._fallback_text(p)
                return
            p = conv
        try:
            import docx
            from docx.oxml.ns import qn
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            d = docx.Document(lp(p))
        except Exception as e:
            raise UnsupportedError(f"cannot open Word file (encrypted or corrupt?): {e}")
        units: list[Word] = []
        n = 0

        def add(text, ref, row=-1, col=-1):
            nonlocal n
            text = re.sub(r"\s+", " ", text or "").strip()
            if text:
                units.append(Word(text=text, box=None, conf=1.0, source="native", ref=ref, row=row, col=col, para=n))
                n += 1

        for si, sec in enumerate(d.sections):
            for pi, para in enumerate(sec.header.paragraphs):
                add(para.text, f"hdr{si}.{pi}")
        ti = 0
        for ch in d.element.body.iterchildren():
            if ch.tag == qn("w:p"):
                add(Paragraph(ch, d).text, f"p{n}")
            elif ch.tag == qn("w:tbl"):
                tbl = Table(ch, d)
                seen = set()
                for ri, row in enumerate(tbl.rows):
                    for ci, cell in enumerate(row.cells):
                        if cell._tc in seen:          # merged cell already emitted (hold the element, not id())
                            continue
                        seen.add(cell._tc)
                        add(cell.text, f"t{ti}:r{ri}c{ci}", ri, ci)
                ti += 1
        for k, tx in enumerate(d.element.body.iter(qn("w:txbxContent"))):
            for pp in tx.iter(qn("w:p")):
                add("".join(t.text or "" for t in pp.iter(qn("w:t"))), f"txbx{k}")
        for si, sec in enumerate(d.sections):
            for pi, para in enumerate(sec.footer.paragraphs):
                add(para.text, f"ftr{si}.{pi}")
        self.units = units
        try:
            for rel in d.part.rels.values():
                if "image" in rel.reltype:
                    self.images.append(rel.target_part.blob)
        except Exception:
            pass

    @staticmethod
    def _fallback_text(p: Path) -> list[Word]:
        raw = p.read_bytes()
        if p.suffix.lower() == ".rtf":
            s = raw.decode("latin-1", "ignore")
            s = re.sub(r"\\par[d]?", "\n", s)
            s = re.sub(r"\\'[0-9a-f]{2}", " ", s)
            s = re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", s)
            parts = [x.strip() for x in s.splitlines() if x.strip()]
        else:   # legacy .doc without LibreOffice: best-effort printable runs
            s = raw.decode("utf-16le", "ignore") if raw[:2] in (b"\xff\xfe",) else raw.decode("latin-1", "ignore")
            parts = [x.strip() for x in re.findall(r"[ -~\u00c0-\u024f]{6,}", s)]
        return [Word(text=t, source="native", ref=f"p{i}", para=i) for i, t in enumerate(parts)]

    def native_words(self, i: int, scale: float, rot: int = 0) -> list[Word]:
        return list(self.units)

    def embedded_images(self, i: int = 0) -> list[Image.Image]:
        import io
        out = []
        for b in self.images:
            try:
                out.append(Image.open(io.BytesIO(b)).convert("RGB"))
            except Exception:
                continue
        return out


def _fmt_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


class XlsxDocument(BaseDocument):
    """Each sheet is a 'page'; every non-empty cell is a unit with its address."""
    kind = "xlsx"
    MAX_CELLS = 250_000

    def __init__(self, path: Path, tmp: Optional[Path] = None):
        super().__init__(path)
        self.sheets: list[tuple[str, list[Word]]] = []
        p = path
        ext = path.suffix.lower()
        if ext in (".xls", ".ods"):
            try:
                if ext == ".xls":
                    self._load_xls(path)
                    return
            except ImportError:
                pass
            conv = convert_with_soffice(path, "xlsx", tmp or Path(tempfile.gettempdir()) / "docnamer_conv")
            if conv is None:
                raise UnsupportedError(f"{ext} needs xlrd (.xls) or LibreOffice")
            p = conv
        self._load_xlsx(p)

    def _load_xlsx(self, p: Path):
        import openpyxl
        from openpyxl.utils import get_column_letter
        try:
            wb = openpyxl.load_workbook(lp(p), read_only=True, data_only=True)
        except Exception as e:
            raise UnsupportedError(f"cannot open workbook (encrypted or corrupt?): {e}")
        total = 0
        for ws in wb.worksheets:
            cells = []
            for row in ws.iter_rows():
                for c in row:
                    if getattr(c, "value", None) is None:
                        continue
                    t = _fmt_cell(c.value)
                    if t:
                        coord = f"{get_column_letter(c.column)}{c.row}"
                        cells.append(Word(t, None, 1.0, "native", f"{ws.title}!{coord}", c.row - 1, c.column - 1))
                        total += 1
                if total > self.MAX_CELLS:
                    break
            self.sheets.append((ws.title, cells))
        wb.close()
        self.page_count = max(1, len(self.sheets))

    def _load_xls(self, p: Path):
        import xlrd
        from openpyxl.utils import get_column_letter
        wb = xlrd.open_workbook(str(p))
        for sh in wb.sheets():
            cells = []
            for r in range(sh.nrows):
                for c in range(sh.ncols):
                    v = sh.cell_value(r, c)
                    if sh.cell_type(r, c) == xlrd.XL_CELL_DATE:
                        v = xlrd.xldate_as_datetime(v, wb.datemode)
                    t = _fmt_cell(v)
                    if t:
                        cells.append(Word(t, None, 1.0, "native", f"{sh.name}!{get_column_letter(c + 1)}{r + 1}", r, c))
            self.sheets.append((sh.name, cells))
        self.page_count = max(1, len(self.sheets))

    def native_words(self, i: int, scale: float, rot: int = 0) -> list[Word]:
        return list(self.sheets[i][1]) if i < len(self.sheets) else []

    def page_label(self, i: int) -> str:
        return self.sheets[i][0] if i < len(self.sheets) else str(i + 1)


class CsvDocument(XlsxDocument):
    kind = "csv"

    def __init__(self, path: Path, tmp: Optional[Path] = None):
        BaseDocument.__init__(self, path)
        from openpyxl.utils import get_column_letter
        raw = None
        for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
            try:
                raw = path.read_text(encoding=enc)
                break
            except (UnicodeError, OSError):
                continue
        raw = raw or ""
        try:
            dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        cells = []
        for r, row in enumerate(csv.reader(raw.splitlines(), dialect)):
            for c, v in enumerate(row):
                v = v.strip()
                if v:
                    cells.append(Word(v, None, 1.0, "native", f"{path.stem}!{get_column_letter(c + 1)}{r + 1}", r, c))
            if len(cells) > self.MAX_CELLS:
                break
        self.sheets = [(path.stem, cells)]
        self.page_count = 1


def open_document(path: Path | str, password_cb=None, tmp: Optional[Path] = None) -> BaseDocument:
    path = Path(path)
    k = kind_of(path)
    if k == "pdf":
        return PdfDocument(path, password_cb)
    if k == "image":
        return ImageDocument(path)
    if k == "docx":
        return DocxDocument(path, tmp)
    if k == "xlsx":
        return XlsxDocument(path, tmp)
    if k == "csv":
        return CsvDocument(path, tmp)
    raise UnsupportedError(f"unsupported file type: {path.suffix}")
