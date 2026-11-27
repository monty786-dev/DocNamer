"""Handwriting & font library.

Every sample is a crop + a verified label.  `unit` says what the label is:
  word - a whole word (shabd), e.g. "रमेश" or "Sharma"
  char - one user-perceived letter (akshar / grapheme), e.g. "क", "त्र", "A", "7"
Nearest-neighbour search (HOG embeddings) suggests labels for new crops; a vocabulary and a
"corrections" memory learn from the user; datasets export to PaddleOCR / TrOCR formats.
"""
from __future__ import annotations

import difflib
import json
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from .textutil import graphemes, script_of

_HOG_WORD = cv2.HOGDescriptor((128, 32), (16, 16), (8, 8), (8, 8), 9)
_HOG_CHAR = cv2.HOGDescriptor((48, 48), (16, 16), (8, 8), (8, 8), 9)


def _fit(g: Image.Image, W: int, H: int) -> np.ndarray:
    w, h = g.size
    s = min(W / max(w, 1), H / max(h, 1))
    g = g.resize((max(1, int(w * s)), max(1, int(h * s))), Image.BILINEAR)
    canvas = Image.new("L", (W, H), 255)
    canvas.paste(g, ((W - g.size[0]) // 2, (H - g.size[1]) // 2))
    return np.asarray(canvas)


def _tight(img: Image.Image) -> Image.Image:
    """Crop to the ink so position / margins don't matter."""
    g = np.asarray(img.convert("L"))
    thr = min(200, int(np.percentile(g, 50)) - 30) if g.size else 128
    ys, xs = np.where(g < max(60, thr))
    if len(xs) == 0:
        return img.convert("L")
    return img.convert("L").crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))


def embed(img: Image.Image, unit: str = "word") -> np.ndarray:
    """L2-normalised HOG embedding. Ink-tight crop first; letters use a square canvas."""
    g = _tight(img)
    if unit == "char":
        v = _HOG_CHAR.compute(_fit(g, 48, 48)).flatten().astype(np.float32)
    else:
        v = _HOG_WORD.compute(_fit(g, 128, 32)).flatten().astype(np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def infer_unit(label: str) -> str:
    return "char" if len(graphemes(label.strip())) == 1 else "word"


class Library:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        (self.root / "samples").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.root / "library.db"), check_same_thread=False)
        self.lock = threading.RLock()
        self._cache: dict = {}
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, label TEXT,
              image TEXT, source_doc TEXT, page INTEGER, bbox TEXT, profile TEXT, conf_before REAL,
              verified INTEGER, created TEXT, emb BLOB);
            CREATE TABLE IF NOT EXISTS profiles(name TEXT PRIMARY KEY, kind TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS vocab(word TEXT PRIMARY KEY, count INTEGER);
            CREATE TABLE IF NOT EXISTS corrections(wrong TEXT PRIMARY KEY, right TEXT, count INTEGER);
            """)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(samples)")}
            if "unit" not in cols:                      # migrate libraries created by older versions
                self.db.execute("ALTER TABLE samples ADD COLUMN unit TEXT DEFAULT 'word'")
            if "script" not in cols:
                self.db.execute("ALTER TABLE samples ADD COLUMN script TEXT DEFAULT ''")
            self.db.commit()
            if "unit" not in cols:
                self._rebuild_embeddings()

    def _rebuild_embeddings(self) -> None:
        """Old rows: derive unit/script from the label and recompute embeddings from the saved crops."""
        for sid, label, image in self.db.execute("SELECT id,label,image FROM samples").fetchall():
            unit = infer_unit(label or "")
            p = self.root / (image or "")
            emb = b""
            if p.exists():
                with Image.open(p) as im:
                    emb = embed(im, unit).tobytes()
            self.db.execute("UPDATE samples SET unit=?, script=?, emb=? WHERE id=?", (unit, script_of(label or ""), emb, sid))
        self.db.commit()

    # ---- samples ------------------------------------------------------------------
    def add_sample(self, img: Image.Image, label: str, kind: str = "handwriting", profile: str = "default",
                   source_doc: str = "", page: int = 0, bbox=None, conf_before: float = 0.0,
                   verified: bool = True, unit: str = "", script: str = "") -> int:
        kind = "font" if kind == "font" else "handwriting"
        label = label.strip()
        unit = unit if unit in ("char", "word") else infer_unit(label)
        script = script or script_of(label)
        emb = embed(img, unit)
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO profiles VALUES(?,?,?)", (profile, kind, datetime.now().isoformat()))
            cur = self.db.execute(
                "INSERT INTO samples(kind,label,image,source_doc,page,bbox,profile,conf_before,verified,created,emb,unit,script)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, label, "", source_doc, page, json.dumps(bbox), profile, conf_before, int(verified),
                 datetime.now().isoformat(), emb.tobytes(), unit, script))
            sid = cur.lastrowid
            rel = f"samples/{kind}_{unit}_{sid:07d}.png"
            img.convert("RGB").save(self.root / rel)
            self.db.execute("UPDATE samples SET image=? WHERE id=?", (rel, sid))
            for wd in label.split():
                self.add_vocab(wd, commit=False)
            self.db.commit()
            self._cache = {}
        return sid

    def list_samples(self, kind: str = "", profile: str = "", unit: str = "", limit: int = 500) -> list[dict]:
        q, a = "SELECT id,kind,label,image,source_doc,page,profile,conf_before,verified,created,unit,script FROM samples WHERE 1=1", []
        for col, val in (("kind", kind), ("profile", profile), ("unit", unit)):
            if val:
                q += f" AND {col}=?"
                a.append(val)
        q += " ORDER BY id DESC LIMIT ?"
        a.append(limit)
        with self.lock:
            rows = self.db.execute(q, a).fetchall()
        keys = ["id", "kind", "label", "image", "source_doc", "page", "profile", "conf_before", "verified", "created", "unit", "script"]
        return [dict(zip(keys, r)) for r in rows]

    def count(self, unit: str = "") -> int:
        with self.lock:
            if unit:
                return self.db.execute("SELECT COUNT(*) FROM samples WHERE verified=1 AND unit=?", (unit,)).fetchone()[0]
            return self.db.execute("SELECT COUNT(*) FROM samples WHERE verified=1").fetchone()[0]

    def update_label(self, sid: int, label: str) -> None:
        with self.lock:
            r = self.db.execute("SELECT image FROM samples WHERE id=?", (sid,)).fetchone()
            unit = infer_unit(label)
            emb = b""
            if r and (self.root / r[0]).exists():
                with Image.open(self.root / r[0]) as im:
                    emb = embed(im, unit).tobytes()
            self.db.execute("UPDATE samples SET label=?, unit=?, script=?, emb=?, verified=1 WHERE id=?",
                            (label.strip(), unit, script_of(label), emb, sid))
            self.db.commit()
            self._cache = {}

    def delete_sample(self, sid: int) -> None:
        with self.lock:
            r = self.db.execute("SELECT image FROM samples WHERE id=?", (sid,)).fetchone()
            if r:
                (self.root / r[0]).unlink(missing_ok=True)
            self.db.execute("DELETE FROM samples WHERE id=?", (sid,))
            self.db.commit()
            self._cache = {}

    # ---- profiles --------------------------------------------------------------------
    def profiles(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT p.name,p.kind,(SELECT COUNT(*) FROM samples s WHERE s.profile=p.name) FROM profiles p ORDER BY p.name").fetchall()
        return [{"name": n, "kind": k, "samples": c} for n, k, c in rows]

    def rename_profile(self, old: str, new: str) -> None:
        with self.lock:
            self.db.execute("UPDATE profiles SET name=? WHERE name=?", (new, old))
            self.db.execute("UPDATE samples SET profile=? WHERE profile=?", (new, old))
            self.db.commit()

    def merge_profiles(self, src: str, dst: str) -> None:
        with self.lock:
            self.db.execute("UPDATE samples SET profile=? WHERE profile=?", (dst, src))
            self.db.execute("DELETE FROM profiles WHERE name=?", (src,))
            self.db.commit()
            self._cache = {}

    def delete_profile(self, name: str) -> None:
        for s in self.list_samples(profile=name, limit=10**9):
            self.delete_sample(s["id"])
        with self.lock:
            self.db.execute("DELETE FROM profiles WHERE name=?", (name,))
            self.db.commit()

    # ---- recognition helpers -------------------------------------------------------------
    def _matrix(self, unit: str):
        if unit not in self._cache:
            with self.lock:
                rows = self.db.execute("SELECT label, profile, emb FROM samples WHERE verified=1 AND unit=? AND length(emb)>0",
                                       (unit,)).fetchall()
            if rows:
                M = np.vstack([np.frombuffer(r[2], dtype=np.float32) for r in rows])
                self._cache[unit] = (M, [r[0] for r in rows], [r[1] for r in rows])
            else:
                self._cache[unit] = (None, [], [])
        return self._cache[unit]

    def suggest(self, img: Image.Image, k: int = 5, min_sim: float = 0.55, unit: str = "word") -> list[tuple[str, float]]:
        """Nearest labels (best similarity per label) among samples of the same unit."""
        M, labels, _ = self._matrix(unit)
        if M is None:
            return []
        sims = M @ embed(img, unit)
        out, seen = [], set()
        for i in np.argsort(-sims)[: 80]:
            if sims[i] < min_sim:
                break
            if labels[i] not in seen:
                seen.add(labels[i])
                out.append((labels[i], float(sims[i])))
            if len(out) >= k:
                break
        return out

    def rerank(self, img: Image.Image, cands: list) -> list:
        """cands: [[text, conf, source], ...] -> same list with library-based confidence bonus, best first."""
        nn = {t: s for t, s in self.suggest(img, 5)}
        out = []
        for t, c, src in cands:
            bonus = 0.0
            if t in nn:
                bonus += 0.15 * nn[t]
            if t and all(self.in_vocab(w) for w in t.split()):
                bonus += 0.05
            out.append([t, min(1.0, c + bonus), src])
        return sorted(out, key=lambda x: -x[1])

    # ---- vocabulary & corrections ----------------------------------------------------------
    def add_vocab(self, word: str, commit: bool = True) -> None:
        word = word.strip()
        if len(word) < 2:
            return
        with self.lock:
            self.db.execute("INSERT INTO vocab VALUES(?,1) ON CONFLICT(word) DO UPDATE SET count=count+1", (word,))
            if commit:
                self.db.commit()

    def in_vocab(self, word: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM vocab WHERE word=?", (word,)).fetchone() is not None

    def spell_snap(self, text: str, cutoff: float = 0.82) -> str:
        with self.lock:
            vocab = [r[0] for r in self.db.execute("SELECT word FROM vocab").fetchall()]
        if not vocab:
            return text
        out = []
        for w in text.split():
            m = difflib.get_close_matches(w, vocab, n=1, cutoff=cutoff)
            out.append(m[0] if m and w not in vocab else w)
        return " ".join(out)

    def add_correction(self, wrong: str, right: str) -> None:
        if not wrong or wrong == right:
            return
        with self.lock:
            self.db.execute("INSERT INTO corrections VALUES(?,?,1) ON CONFLICT(wrong) DO UPDATE SET right=excluded.right, count=count+1",
                            (wrong, right))
            self.db.commit()

    def lookup_correction(self, wrong: str, similar: float = 0.9) -> Optional[str]:
        with self.lock:
            rows = self.db.execute("SELECT wrong,right FROM corrections").fetchall()
        d = dict(rows)
        if wrong in d:
            return d[wrong]
        m = difflib.get_close_matches(wrong, list(d), n=1, cutoff=similar)
        return d[m[0]] if m else None

    # ---- export / import ------------------------------------------------------------------------
    def export_dataset(self, out_dir: Path | str, fmt: str = "paddle", kind: str = "", unit: str = "word") -> int:
        """paddle: images/ + rec_gt.txt ("images/x.png<TAB>label").  trocr: images/ + metadata.jsonl.
        unit='word' (default) exports word-level samples - the right granularity for text-line recognisers."""
        out = Path(out_dir)
        (out / "images").mkdir(parents=True, exist_ok=True)
        lines = []
        for r in self.list_samples(kind=kind, unit=unit, limit=10**9):
            if not r["verified"] or not r["label"]:
                continue
            name = Path(r["image"]).name
            shutil.copy2(self.root / r["image"], out / "images" / name)
            if fmt == "trocr":
                lines.append(json.dumps({"file_name": f"images/{name}", "text": r["label"]}, ensure_ascii=False))
            else:
                lines.append(f"images/{name}\t{r['label']}")
        (out / ("metadata.jsonl" if fmt == "trocr" else "rec_gt.txt")).write_text("\n".join(lines), encoding="utf-8")
        return len(lines)

    def export_zip(self, zip_path: Path | str) -> None:
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "library.db"
            with self.lock:
                dst = sqlite3.connect(str(snap))
                self.db.backup(dst)
                dst.close()
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(snap, "library.db")
                for p in (self.root / "samples").glob("*.png"):
                    z.write(p, f"samples/{p.name}")

    def import_zip(self, zip_path: Path | str) -> int:
        n = 0
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(td)
            src = sqlite3.connect(str(Path(td) / "library.db"))
            cols = {r[1] for r in src.execute("PRAGMA table_info(samples)")}
            q = ("SELECT kind,label,image,source_doc,page,bbox,profile,conf_before,verified,"
                 + ("unit" if "unit" in cols else "''") + " FROM samples")
            for kind, label, image, source_doc, page, bbox, profile, cb, ver, unit in src.execute(q):
                p = Path(td) / image
                if p.exists():
                    with Image.open(p) as im:
                        self.add_sample(im.copy(), label, kind, profile, source_doc, page or 0,
                                        json.loads(bbox) if bbox else None, cb or 0.0, bool(ver), unit or "")
                    n += 1
            for w, c in src.execute("SELECT word,count FROM vocab"):
                self.add_vocab(w, commit=False)
            for wr, rt, c in src.execute("SELECT wrong,right,count FROM corrections"):
                self.add_correction(wr, rt)
            self.db.commit()
            src.close()
        return n

    def close(self):
        with self.lock:
            self.db.close()
