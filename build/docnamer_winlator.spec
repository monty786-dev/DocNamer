# PyInstaller spec - DocNamer Lite for Winlator / Wine (x64, Tkinter, CPU only). onedir on purpose:
# a onefile exe unpacks to %TEMP% on every start, which is very slow under Box64 emulation.
# Build:  pyinstaller --noconfirm --clean build/docnamer_winlator.spec
from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas, binaries, hidden = [], [], ["pytesseract", "pdfminer.high_level", "docx", "openpyxl", "xlrd", "pillow_heif",
                                   "tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.colorchooser", "tkinter.simpledialog"]
for pkg in ("rapidocr_onnxruntime", "onnxruntime", "pypdfium2", "pypdfium2_raw", "cv2", "reportlab"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hidden += h
    except Exception as e:
        print("spec: skipping", pkg, e)
datas += collect_data_files("docx")

a = Analysis([os.path.join(ROOT, "run_lite.py"), os.path.join(ROOT, "run_cli.py")],
             pathex=[ROOT], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=["PySide6", "shiboken6", "torch", "transformers", "matplotlib", "scipy", "pandas"], noarchive=False)
pyz = PYZ(a.pure)
gui = EXE(pyz, [s for s in a.scripts if s[0] != "run_cli"], [], exclude_binaries=True, name="DocNamer-Lite", console=False)
cli = EXE(pyz, [s for s in a.scripts if s[0] != "run_lite"], [], exclude_binaries=True, name="docnamer-cli", console=True)
coll = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name="DocNamer-winlator")
