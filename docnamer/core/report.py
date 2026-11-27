"""CSV report of every action + undo (deletes only files this tool created inside the output folder)."""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from .env import lp
from .models import FileResult

COLUMNS = ["time", "status", "original_path", "new_path", "new_name", "fields", "confidence", "engine",
           "pages", "template", "marked", "text", "message", "sha"]


class Report:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with open(self.path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(COLUMNS)

    def add(self, r: FileResult) -> None:
        row = [time.strftime("%Y-%m-%d %H:%M:%S"), r.status, r.path, r.new_path, r.new_name,
               json.dumps({k: v.text for k, v in r.fields.items()}, ensure_ascii=False),
               f"{r.confidence:.3f}", r.engine, ",".join(str(p + 1) for p in r.pages_read), r.template,
               "|".join(r.marked_paths), "|".join(p for p in (r.text_path, r.json_path, r.pdf_path) if p),
               r.message, r.sha]
        with open(self.path, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(row)


def undo_report(report_path: Path | str, output_dir: Path | str) -> dict:
    """Delete outputs listed in the report (originals are never touched). Returns counts."""
    rp, out = Path(report_path), Path(output_dir).resolve()
    removed = missing = refused = 0
    with open(rp, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("status") != "done":
                continue
            paths = [row.get("new_path", "")] + (row.get("marked", "") or "").split("|") + (row.get("text", "") or "").split("|")
            for p in paths:
                if not p:
                    continue
                pp = Path(p)
                try:
                    inside = out in pp.resolve().parents
                except OSError:
                    inside = False
                if not inside:
                    refused += 1
                    continue
                if pp.exists():
                    pp.unlink()
                    removed += 1
                else:
                    missing += 1
    rp.rename(rp.with_name(f"{rp.stem}.undone-{time.strftime('%Y%m%d-%H%M%S')}{rp.suffix}"))
    return {"removed": removed, "missing": missing, "refused": refused}
