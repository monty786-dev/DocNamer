# PyInstaller spec - DocNamer Full (Windows x64, PySide6 GUI + CLI in one folder).
# Build:  pyinstaller --noconfirm --clean build/docnamer_full.spec
from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas, binaries, hidden = [], [], ["pytesseract", "pdfminer.high_level", "docx", "openpyxl", "xlrd", "pillow_heif"]
for pkg in ("rapidocr_onnxruntime", "onnxruntime", "pypdfium2", "pypdfium2_raw", "cv2", "reportlab"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hidden += h
    except Exception as e:                      # optional package missing at build time
        print("spec: skipping", pkg, e)
datas += collect_data_files("docx")

a = Analysis([os.path.join(ROOT, "run_full.py"), os.path.join(ROOT, "run_cli.py")],
             pathex=[ROOT], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "torch", "transformers", "matplotlib", "scipy", "pandas"], noarchive=False)
pyz = PYZ(a.pure)
gui = EXE(pyz, [s for s in a.scripts if s[0] != "run_cli"], [], exclude_binaries=True, name="DocNamer", console=False)
cli = EXE(pyz, [s for s in a.scripts if s[0] != "run_full"], [], exclude_binaries=True, name="docnamer-cli", console=True)
coll = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name="DocNamer-full")
