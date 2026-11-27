"""Run the pipeline on a worker thread and talk to ANY GUI through a thread-safe event queue.

Events (tuples):  ("log", msg) | ("progress", done, total, msg) | ("analyzed", results)
                  ("finished", results) | ("error", msg) | ("cancelled",)
                  ("review", item, done_event, box)    -> GUI sets box["decision"] then done_event.set()
                  ("password", path, done_event, box)  -> GUI sets box["password"] then done_event.set()
"""
from __future__ import annotations

import logging
import queue
import threading
import traceback
from pathlib import Path
from typing import Optional

from .models import FileResult
from .pipeline import Cancelled, Pipeline, RunControl
from .settings import Settings

log = logging.getLogger("docnamer.runner")


class Runner:
    def __init__(self, settings: Settings, data_dir: Path | str):
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.control = RunControl()
        self.pipeline = Pipeline(settings, data_dir, review_cb=self._ask_review, password_cb=self._ask_password,
                                 log_cb=lambda m: self.events.put(("log", m)))
        self.results: list[FileResult] = []
        self.thread: Optional[threading.Thread] = None

    @property
    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    # -- callbacks executed on the worker thread, answered by the GUI thread ----------------------
    def _ask(self, kind: str, payload, key: str):
        ev, box = threading.Event(), {}
        self.events.put((kind, payload, ev, box))
        while not ev.wait(0.2):
            if self.control.cancelled:
                return None
        return box.get(key)

    def _ask_review(self, item):
        return self._ask("review", item, "decision")

    def _ask_password(self, path):
        return self._ask("password", str(path), "password")

    # -- jobs ------------------------------------------------------------------------------------------
    def start(self, job: str, reprocess: bool = False) -> None:
        """job: preview (dry-run) | analyze (no copying yet) | run (analyze + copy) | finish (commit self.results)"""
        if self.busy:
            return
        self.control = RunControl()
        self.thread = threading.Thread(target=self._work, args=(job, reprocess), daemon=True)
        self.thread.start()

    def _work(self, job: str, reprocess: bool) -> None:
        p = self.pipeline
        prog = lambda d, t, m: self.events.put(("progress", d, t, m))
        try:
            if job == "preview":
                self.results = p.run(dry_run=True, control=self.control, progress=prog, reprocess=True)
                self.events.put(("analyzed", self.results))
            elif job == "analyze":
                self.results = p.run(dry_run=False, commit_now=False, control=self.control, progress=prog, reprocess=reprocess)
                self.events.put(("analyzed", self.results))
            elif job == "run":
                self.results = p.run(dry_run=False, commit_now=True, control=self.control, progress=prog, reprocess=reprocess)
                self.events.put(("finished", self.results))
            elif job == "finish":
                p.finish(self.results, progress=prog)
                self.events.put(("finished", self.results))
        except Cancelled:
            self.events.put(("cancelled",))
        except Exception as e:
            log.exception("runner failed")
            self.events.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"))

    def pause(self): self.control.pause()
    def resume(self): self.control.resume()
    def cancel(self): self.control.cancel()

    def pending_teach(self) -> list[tuple[FileResult, object]]:
        return [(r, it) for r in self.results for it in list(r.teach)]

    def pending_review(self) -> list[tuple[FileResult, object]]:
        return [(r, it) for r in self.results if r.status == "review" for it in list(r.review)]
