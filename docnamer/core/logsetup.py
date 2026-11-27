"""Structured (JSON-lines) logging with rotation."""
from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"t": self.formatTime(record, "%Y-%m-%d %H:%M:%S"), "level": record.levelname,
             "logger": record.name, "msg": record.getMessage()}
        extra = getattr(record, "ctx", None)
        if extra:
            d.update(extra)
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


def setup_logging(data_dir: Path, level: int = logging.INFO, console: bool = True) -> logging.Logger:
    log = logging.getLogger("docnamer")
    if log.handlers:
        return log
    log.setLevel(level)
    logs = Path(data_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(logs / "docnamer.log", maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    log.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(ch)
    return log
