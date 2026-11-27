"""Download the optional data the app can use. Nothing is committed to git; CI runs this at build time.

  python scripts/fetch_models.py --tess hin,eng,osd            # Hindi (+English, +orientation) for Tesseract
  python scripts/fetch_models.py --tess hin --dest "D:\\DocNamer\\data\\models\\tessdata"

Tesseract language packs come from the official tesseract-ocr/tessdata_fast repository (Apache-2.0).
RapidOCR's English/Chinese models are already inside the `rapidocr-onnxruntime` wheel.
Handwriting: no model is downloaded - the app learns YOUR handwriting from the Teach dialog
(Library > Teach handwriting).  To use your own CRNN/CTC model put rec.onnx + charset.txt in
<data>/models/handwriting/ (see README)."""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPOS = [
    "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata",
    "https://github.com/tesseract-ocr/tessdata_fast/raw/master/{lang}.traineddata",
    "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{lang}.traineddata",
    "https://github.com/tesseract-ocr/tessdata/raw/main/{lang}.traineddata",
]
MIN_BYTES = 100_000          # every real traineddata file is far bigger; guards against HTML error pages


def download(url: str, dest: Path, tries: int = 3) -> bool:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DocNamer-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if len(data) < MIN_BYTES:
                return False
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2 * (i + 1))
    return False


def fetch_tess(langs: list[str], dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    bad = 0
    for lang in langs:
        target = dest / f"{lang}.traineddata"
        if target.exists() and target.stat().st_size > MIN_BYTES:
            print(f"  have  {target.name}")
            continue
        if any(download(u.format(lang=lang), target) for u in REPOS):
            print(f"  got   {target.name}  ({target.stat().st_size // 1024} KB)")
        else:
            print(f"  FAILED {lang}: download it manually from https://github.com/tesseract-ocr/tessdata_fast "
                  f"and put {lang}.traineddata in {dest}", file=sys.stderr)
            bad += 1
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tess", default="eng,osd,hin", help="comma separated Tesseract languages (default: eng,osd,hin)")
    ap.add_argument("--dest", default="data/models/tessdata", help="target folder (default: data/models/tessdata)")
    ap.add_argument("--flavor", default="", help="ignored (kept so CI can pass it)")
    a = ap.parse_args()
    print(f"Tesseract language data -> {a.dest}")
    return 1 if fetch_tess([x.strip() for x in a.tess.split(",") if x.strip()], Path(a.dest)) else 0


if __name__ == "__main__":
    sys.exit(main())
