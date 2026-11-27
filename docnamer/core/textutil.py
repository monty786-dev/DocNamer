"""Unicode / Hindi (Devanagari) helpers: graphemes, script detection, digits, month names,
transliteration to Latin, and the on-screen Devanagari keypad layout."""
from __future__ import annotations

import re
import unicodedata

VIRAMA = "\u094d"
ZWJ, ZWNJ = "\u200d", "\u200c"


def to_ascii_digits(s: str) -> str:
    """१२३ / ١٢٣ / ১২৩ ... -> 123 (any Unicode decimal digit)."""
    out = []
    for ch in s or "":
        if ch.isdigit() and not ch.isascii():
            try:
                out.append(str(unicodedata.digit(ch)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(ch)
    return "".join(out)


def graphemes(s: str) -> list[str]:
    """Split into user-perceived characters: base + marks (matras, anusvara, nukta), and
    conjuncts joined by virama (क्ष = क + ् + ष is ONE akshar)."""
    clusters: list[str] = []
    for ch in unicodedata.normalize("NFC", s or ""):
        if not clusters:
            clusters.append(ch)
            continue
        prev = clusters[-1]
        if unicodedata.category(ch).startswith("M") or ch in (ZWJ, ZWNJ) or prev.endswith((VIRAMA, ZWJ)):
            clusters[-1] = prev + ch
        else:
            clusters.append(ch)
    return clusters


def is_devanagari_char(ch: str) -> bool:
    return "\u0900" <= ch <= "\u097f" or "\ua8e0" <= ch <= "\ua8ff"


def script_of(s: str) -> str:
    """devanagari | latin | digits | mixed | other | empty"""
    letters = [c for c in s if c.isalpha() or unicodedata.category(c).startswith("M")]
    if not letters:
        return "digits" if any(c.isdigit() for c in s) else "empty"
    dev = sum(is_devanagari_char(c) for c in letters)
    lat = sum(c.isascii() and c.isalpha() for c in letters)
    if dev and not lat:
        return "devanagari"
    if lat and not dev:
        return "latin"
    return "mixed" if dev and lat else "other"


def is_letters(text: str) -> bool:
    """Letters in ANY script, including combining marks (Hindi matras): words with . ' - and spaces."""
    t = (text or "").strip()
    if not t:
        return False
    has_letter = False
    for ch in t:
        cat = unicodedata.category(ch)
        if cat.startswith("L"):
            has_letter = True
        elif not (cat.startswith("M") or ch in " .'-\u2019" or ch in (ZWJ, ZWNJ)):
            return False
    return has_letter


# ---- Hindi dates -----------------------------------------------------------------------------
HINDI_MONTHS = {
    "जनवरी": "January", "फ़रवरी": "February", "फरवरी": "February", "मार्च": "March", "अप्रैल": "April",
    "अप्रेल": "April", "मई": "May", "जून": "June", "जुलाई": "July", "अगस्त": "August",
    "सितंबर": "September", "सितम्बर": "September", "अक्टूबर": "October", "अक्तूबर": "October",
    "नवंबर": "November", "नवम्बर": "November", "दिसंबर": "December", "दिसम्बर": "December",
}


def normalize_date_text(s: str) -> str:
    """Hindi digits -> ASCII and Hindi month names -> English, so strptime can parse it."""
    s = unicodedata.normalize("NFC", to_ascii_digits(s or ""))
    for hi, en in HINDI_MONTHS.items():
        s = s.replace(unicodedata.normalize("NFC", hi), en)
    return s


# ---- transliteration (for ASCII-only file names) ----------------------------------------------------
_IND_VOW = {"अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri", "ए": "e", "ऐ": "ai",
            "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_MATRA = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o",
          "ौ": "au", "ॉ": "o", "ॅ": "e"}
_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
         "ञ": "ny", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d",
         "ध": "dh", "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r",
         "ल": "l", "ळ": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
         "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "d", "ढ़": "dh", "फ़": "f"}


def transliterate(s: str) -> str:
    """Devanagari -> Latin ('रमेश शर्मा' -> 'ramesh sharma'). Simple, deterministic, schwa-final-deleted."""
    s = unicodedata.normalize("NFC", to_ascii_digits(s or ""))
    # NFC composes nukta forms (क + ़ stays separate in NFC); fold them into the table keys
    s = (s.replace("क\u093c", "क़").replace("ख\u093c", "ख़").replace("ग\u093c", "ग़").replace("ज\u093c", "ज़")
         .replace("ड\u093c", "ड़").replace("ढ\u093c", "ढ़").replace("फ\u093c", "फ़"))
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in _CONS:
            out.append(_CONS[ch])
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt in _MATRA:
                out.append(_MATRA[nxt])
                i += 2
                continue
            if nxt == VIRAMA:
                i += 2
                continue
            after = s[i + 1] if i + 1 < n else ""
            word_end = not (after in _CONS or after in _IND_VOW or after in ("ं", "ँ", "ः") or after in _MATRA)
            out.append("" if word_end else "a")
        elif ch in _IND_VOW:
            out.append(_IND_VOW[ch])
        elif ch == "ं" or ch == "ँ":
            out.append("n")
        elif ch == "ः":
            out.append("h")
        elif ch in ("\u093c", ZWJ, ZWNJ, VIRAMA):
            pass
        elif ch == "।":
            out.append(".")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# ---- on-screen Devanagari keypad (for people without a Hindi keyboard) ------------------------------------
DEVANAGARI_KEYBOARD = {
    "स्वर (vowels)": list("अआइईउऊऋएऐओऔ") + ["अं", "अः"],
    "व्यंजन (consonants)": list("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसह") + ["क्ष", "त्र", "ज्ञ", "श्र", "ड़", "ढ़"],
    "मात्राएँ (matras)": list("ािीुूृेैोौ") + ["ं", "ँ", "ः", "्", "़", "ॉ"],
    "अंक (digits)": list("०१२३४५६७८९") + ["।", "-", "/", "."],
}
