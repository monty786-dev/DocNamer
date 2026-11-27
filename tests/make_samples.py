"""Generate a realistic sample folder (no binary fixtures are committed to git)."""
from __future__ import annotations

import io
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf", "arial.ttf"]


def _font(size):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def invoice_image(inv: str, date: str, customer: str, size=(1240, 1754)) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.text((120, 140), "TAX INVOICE", fill="black", font=_font(48))
    f = _font(34)
    y = 300
    for line in (f"Invoice No: {inv}", f"Date: {date}", f"Customer: {customer}", "Total amount payable: 15,400.00",
                 "Thank you for your business"):
        d.text((120, y), line, fill="black", font=f)
        y += 80
    return img


def native_pdf(path: Path, inv: str, date: str, customer: str, pages_extra=0, rotate: int = 0, password: str = ""):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4, encrypt=password or None)
    c.setFont("Helvetica", 14)
    c.drawString(60, 780, "TAX INVOICE")
    c.drawString(60, 740, f"Invoice No: {inv}")
    c.drawString(60, 710, f"Date: {date}")
    c.drawString(60, 680, f"Customer: {customer}")
    c.showPage()
    for i in range(pages_extra):
        c.drawString(60, 780, f"Terms and conditions page {i + 2}")
        c.showPage()
    c.save()
    if rotate:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        for i in range(len(pdf)):
            pdf[i].set_rotation(rotate)
        pdf.save(str(path))


def scanned_pdf(path: Path, images: list[Image.Image]):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    for im in images:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, A4[0], A4[1])
        c.showPage()
    c.save()


def build(root: Path) -> Path:
    from docnamer.core.orientation import rotate_cw, rotate_skew
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    (root / "client_a").mkdir(parents=True)
    (root / "client_b" / "scans").mkdir(parents=True)
    native_pdf(root / "client_a" / "doc001.pdf", "INV-1001", "05/03/2024", "Asha Verma", pages_extra=1)
    native_pdf(root / "client_a" / "doc002.pdf", "INV-1002", "06/03/2024", "Ravi Patel", rotate=90)
    shutil.copy2(root / "client_a" / "doc001.pdf", root / "client_a" / "doc001_copy.pdf")        # exact duplicate
    img = invoice_image("INV-2001", "12 March 2024", "Ramesh Sharma")
    rotate_skew(rotate_cw(img, 270), 2.0).save(root / "client_b" / "scans" / "scan_sideways.png")  # 90 deg + skew
    rotate_cw(invoice_image("INV-2002", "13 March 2024", "Neha Singh"), 180).save(root / "client_b" / "scans" / "scan_upside.jpg")
    scanned_pdf(root / "client_b" / "scanned_two_page.pdf",
                [Image.new("RGB", (1240, 1754), "white"), invoice_image("INV-3001", "20 April 2024", "Kiran Rao")])
    import docx
    d = docx.Document()
    d.add_paragraph("Purchase Order")
    d.add_paragraph("Invoice No: DX-5001")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Customer", "Deepak Joshi"
    t.cell(1, 0).text, t.cell(1, 1).text = "Date", "2024-05-09"
    d.save(root / "client_a" / "order.docx")
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"], ws["B1"] = "Invoice No", "XL-777"
    ws["A2"], ws["B2"] = "Customer", "Meena Iyer"
    wb.save(root / "client_a" / "sheet.xlsx")
    native_pdf(root / "client_b" / "secret.pdf", "INV-9999", "01/01/2024", "Hidden", password="s3cret")
    (root / "client_b" / "notes.txt").write_text("not a supported document")
    return root
