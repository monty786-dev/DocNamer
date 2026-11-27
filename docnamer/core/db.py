"""SQLite job database: remembers finished files (by content hash + settings fingerprint) so runs resume."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


class JobDB:
    def __init__(self, path: Path | str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("""CREATE TABLE IF NOT EXISTS jobs(sha TEXT, fp TEXT, path TEXT, status TEXT,
                               new_path TEXT, result TEXT, updated REAL, PRIMARY KEY(sha, fp, path))""")
            self.db.commit()

    def done(self, sha: str, fp: str, path: str) -> Optional[str]:
        with self.lock:
            r = self.db.execute("SELECT new_path FROM jobs WHERE sha=? AND fp=? AND path=? AND status='done'",
                                (sha, fp, path)).fetchone()
        return r[0] if r else None

    def put(self, sha: str, fp: str, path: str, status: str, new_path: str, result: dict) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?)",
                            (sha, fp, path, status, new_path, json.dumps(result, ensure_ascii=False, default=str), time.time()))
            self.db.commit()

    def clear(self) -> None:
        with self.lock:
            self.db.execute("DELETE FROM jobs")
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()
