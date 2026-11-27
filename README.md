# DocNamer

Offline tool that **reads documents and saves renamed copies** named after what is inside them.
Originals are never changed. Works on folders (and optionally sub-folders) of **PDF, Word, Excel/CSV and images**,
in **English and Hindi (हिन्दी)**, including **handwriting you teach it**.

Two builds from one code base:

| Build | For | GUI |
|---|---|---|
| **DocNamer-full** | Windows 10/11 x64 | PySide6 (dark/light theme) |
| **DocNamer-winlator** | Android via Winlator (Wine + Box64), CPU only | Tkinter, big touch buttons, in-app folder browser |

Both include `docnamer-cli.exe` for automation.

---

## 1. Quick start

**From a release zip:** unzip, run `DocNamer.exe` (or `DocNamer-Lite.exe` in Winlator). Nothing to install.
Settings, templates, handwriting library and logs live in the `data/` folder next to the exe (portable).

**From source (Python 3.11 x64):**
```
pip install -r requirements-full.txt        # or requirements-lite.txt
python run_full.py                           # GUI   (run_lite.py = Tkinter GUI)
python -m docnamer info                      # shows which OCR engines were found
```
Tesseract is needed for **Hindi** and for the best page-rotation detection: install it or put it in `tesseract/` next to the exe.

## 2. The wizard (7 steps)

1. **Folder** – input/output, include sub-folders (+ depth, include/exclude patterns), output structure, duplicate handling.
2. **Orientation** – *Auto-fix orientation is the first option and on by default.* Detects 0/90/180/270° per page and straightens small tilts; also corrects PDF `/Rotate`. Modes: auto / ask when unsure / off. Original files are not rotated (optional corrected copy).
3. **Zones / Mode** – *Zones* (drag rectangles on a sample page, or type X/Y/W/H %), *Anchors* ("read what is right of / below *Invoice No*"), or *Full read* (every letter → text/JSON/searchable PDF). Templates with automatic matching by keywords or page layout.
4. **Multi-page** – which pages (`first`, `last`, `all`, `first_found`, `1,3-5`), strategy (first match / first valid / highest confidence / longest / concatenate), skip blank / cover / QR pages, split multi-document PDFs at blank pages or a keyword, must-contain / must-not-contain filters, what to do when a field is missing.
5. **Naming** – pattern tokens `{field} {original_name} {date} {mdate} {page} {counter:03} {folder} {first_line}` and transforms: `{inv|upper}` `{d|date:%Y%m%d}` `{x|re:(\d+)}` `{y|default:NA}` `{n|translit}` `{k|asciidigits}`. Windows-safe (reserved names, illegal characters, path length). Optional marked image (rectangle / ellipse / underline / highlight, colour, per-letter boxes), text export, searchable PDF.
6. **Preview** – dry run; edit any new name before anything is copied.
7. **Run** – progress, pause/resume/cancel, review queue, **Undo** (deletes only what the run created).

CLI example:
```
docnamer run D:\Scans -o D:\Out --anchor "invoice=Invoice No" --anchor "date=Date|right||date" ^
        --pattern "{invoice}_{date|date:%Y%m%d}" --pages first_found --mark rectangle --dry-run
docnamer apply D:\Out\plan.csv          # after editing the new_name column
docnamer undo D:\Out
```

## 3. Hindi (हिन्दी)

* **Setup:** `python scripts/fetch_models.py --tess hin,eng,osd` (the release zips already contain it). Choose **Text language = `hin`** (or `eng+hin`) in step 1.
  Hindi is read by **Tesseract**; the bundled RapidOCR models cannot read Devanagari, so the app never routes Hindi to them. If the language pack is missing you get a clear message naming the file.
* Text inside Hindi **PDFs (with a text layer), Word and Excel** is read directly - no OCR needed.
* File names can stay in Hindi (`रमेश शर्मा_20240312.pdf`) or become ASCII with `{name|translit}` → `ramesh sharma`.
* Hindi digits (`१२३४`) and dates (`१२ मार्च २०२४`, `१२/०३/२०२४`) are understood; `{k|asciidigits}` converts digits.
* Field format "letters" accepts Hindi vowel signs (matras), which a plain `\w` regex would reject.
* The Teach and review dialogs have an **on-screen Devanagari keypad** (vowels, consonants, matras, digits) for devices without a Hindi keyboard.

## 4. Handwriting - how the app learns *your* writing

No handwriting model is downloaded. Instead the app learns from you:

1. **Teach it.** Open **Library → Teach handwriting…**, or in step 3 select a zone on a sample page and press **Teach handwriting…**, or tick *"After the run, ask me what handwritten fields say"* (step 4), or tick **Save to library** in the *Can't read this* dialog.
2. The app cuts the writing into **words (शब्द)** and, on request, **letters (अक्षर)** and **asks what each piece contains**:
   it shows the crop, pre-fills its best guess, you type (or use the keypad) and press *Save & next*. If you typed a whole line and the number of words matches, the words are paired automatically without questions; otherwise it asks piece by piece. *Teach this word's letters* queues the letter pieces. Choose the **writer/font name** to keep different people apart.
3. **From then on** zones/anchors marked *handwritten* are read by matching against what you taught (words first, then letter-by-letter). Confidence is deliberately conservative: unfamiliar or ambiguous writing gets a low score and goes to the **review queue** (zoomed crop, top-5 candidates, typed answer, *apply to all similar*, *save to library*, *skip*, *use fallback name*, *needs manual*).

Honest limits (measured on synthetic handwriting-like text in my tests): same writing style → 100 % top-1 on taught words; a clearly different style → 92 %, and a drastically different one → ~50 %, so teach several samples per writer. Joined cursive letters cannot be split into letters - teach whole words. Devanagari letter splitting is approximate (a vowel sign may become its own piece); the dialog lets you label pieces as they are.
Training sets export for PaddleOCR (Devanagari-capable) or TrOCR: `docnamer library export-dataset my_set --format paddle`. Optional ONNX CTC model: put `rec.onnx` + `charset.txt` in `data/models/handwriting/`.

CLI teaching: `docnamer teach form.png --zone 0.1,0.2,0.6,0.1 --text "रमेश कुमार" --profile Asha`.

## 5. Winlator (Android)

1. Copy `DocNamer-winlator-win64.zip` to the phone, extract it into a folder Winlator can see.
2. In Winlator open the container → *Start menu → Run* → choose `DocNamer-Lite.exe`.
3. Android storage appears as drive letters (often `Z:` or `D:`). Use the in-app **Browse…** (big buttons) or type the path.
4. The lite profile is automatic: 1 worker, 200 DPI, 12-megapixel cap, no ensemble. Use *first/last page* rules and keep batches modest - OCR under Box64 emulation is several times slower than on a PC.
5. If the window is tiny/huge, change the container screen size or Wine DPI; the GUI uses large fonts by design.

## 6. Build the exe on GitHub

1. Create a repository on github.com, then:
   ```
   git init && git add . && git commit -m "DocNamer" && git branch -M main
   git remote add origin https://github.com/YOURNAME/docnamer.git && git push -u origin main
   ```
2. Open the **Actions** tab → the *Build Windows EXE* run (or **Run workflow**). Download the zips under **Artifacts**.
3. For a download page: `git tag v1.0.0 && git push origin v1.0.0` → zips appear under **Releases**.

The workflow installs the packages, runs the tests, builds both flavours with PyInstaller (onedir), adds Tesseract and the Hindi/English/OSD language data, zips and uploads (+ `frozen-*.txt` with the exact library versions used).
Unsigned exes may trigger Windows SmartScreen / antivirus warnings - normal for PyInstaller builds.

## 7. Project layout

```
docnamer/core/      engine (no UI): readers, orientation, OCR engines, extract, pipeline, naming, library, teach, review, report
docnamer/cli/       command line          docnamer/gui_full/  PySide6          docnamer/gui_lite/  Tkinter (Winlator)
scripts/            fetch_models.py, finetune_trocr.py (experimental)      build/  PyInstaller specs     tests/  unittest suite
```

## 8. What is verified, and what is not

| Area | Status |
|---|---|
| Engine, CLI, naming, orientation, PDF/Word/Excel/CSV readers, marked images, review/undo/resume/duplicates/split/parallel, Hindi text handling, handwriting teaching | **Automated tests (46 tests, Linux, Tesseract English)** |
| Hindi OCR of *scanned* pages with Tesseract | Wiring + error handling tested; **recognition not tested** (no `hin` data / network where this was built) |
| RapidOCR engine | Code written against its documented API; **not run** (not installable where this was built). Falls back to Tesseract automatically |
| Qt and Tk GUIs | **Syntax/static checks only - never launched.** Expect small UI fixes on first run. The event/runner layer they share is tested |
| PyInstaller specs, GitHub workflow, Winlator behaviour | **Not executed** - written from PyInstaller/Actions conventions. First CI run may need small tweaks |
| `fetch_models.py` download URLs | Official Tesseract repositories; **not reachable from the build sandbox** |
| `finetune_trocr.py` | Experimental, untested; English-only model |

## 9. Third-party licences

PyMuPDF is **not** used (AGPL). Libraries: pypdfium2 (Apache-2.0/BSD), pdfminer.six (MIT), python-docx (MIT), openpyxl (MIT), xlrd (BSD), Pillow (HPND), OpenCV (Apache-2.0), onnxruntime (MIT), rapidocr-onnxruntime (Apache-2.0), pytesseract (Apache-2.0), Tesseract + tessdata_fast (Apache-2.0), reportlab (BSD), numpy (BSD), PySide6 (**LGPL-3.0**, full build only; shipped as separate DLLs), Tkinter (PSF).
