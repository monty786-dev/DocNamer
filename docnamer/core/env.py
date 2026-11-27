"""Environment detection: Wine/Winlator, portable data dir, helper binaries."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional


def is_wine() -> bool:
    """True when running under Wine (and therefore Winlator on Android)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        ntdll = ctypes.WinDLL("ntdll")
        getattr(ntdll, "wine_get_version")
        return True
    except (OSError, AttributeError):
        return False


def app_dir() -> Path:
    """Folder beside the exe (frozen) or the repository root (source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """Where PyInstaller unpacked bundled read-only resources."""
    return Path(getattr(sys, "_MEIPASS", app_dir()))


def data_dir(override: Optional[str] = None) -> Path:
    """Portable data directory: settings, models, library, logs live here."""
    p = Path(override or os.environ.get("DOCNAMER_DATA") or (app_dir() / "data"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def lp(path: os.PathLike | str) -> str:
    """Long-path-safe string for Windows file APIs (\\\\?\\ prefix)."""
    s = os.path.abspath(str(path))
    if os.name == "nt" and len(s) >= 240 and not s.startswith("\\\\?\\"):
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
        return "\\\\?\\" + s
    return s


def find_tesseract(custom: str = "") -> Optional[str]:
    cands = [custom] if custom else []
    cands += [str(app_dir() / "tesseract" / "tesseract.exe"),
              str(app_dir() / "tesseract" / "tesseract"),
              r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]
    for c in cands:
        if c and Path(c).exists():
            return c
    return shutil.which("tesseract")


def find_soffice() -> Optional[str]:
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for c in (r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
              str(app_dir() / "LibreOffice" / "program" / "soffice.exe")):
        if Path(c).exists():
            return c
    return None


def drives() -> list[str]:
    """Drive letters visible to the process (Android storage is mapped as Z:/D:/... in Winlator)."""
    if os.name != "nt":
        return ["/"]
    return [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]
