"""Progress reporting, cancellation and result logging.

The core never prints or prompts. It talks to whatever front end is running
(the CLI menu today, a GUI later) through a Reporter, and checks a
CancelToken between actions so a front end can stop a run cleanly.
"""

from __future__ import annotations

import csv
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


class Cancelled(Exception):
    """Raised inside the core when the front end asked to stop."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()


@dataclass
class ActionResult:
    """One row of the results CSV: one thing the tool did (or would do)."""

    step: str
    action: str
    target_type: str          # agenda | category | minutes
    category: str = ""
    target_id: str = ""
    meeting_date: str = ""
    description: str = ""
    status: str = ""          # ok | error | skipped | dry-run
    detail: str = ""
    files: str = ""           # uploads: "agenda: x.pdf | minutes: y.docx"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


LogFn = Callable[[str, str], None]                 # (level, message)
ProgressFn = Callable[[int, int, str], None]       # (done, total, label)
ResultFn = Callable[[ActionResult], None]


class Reporter:
    """Fan-out for log lines, progress updates and per-action results.

    Front ends pass in callbacks; anything left as None is ignored. Every
    result is also kept in memory and written to the run's CSV so nothing
    depends on the front end staying alive.
    """

    def __init__(
        self,
        on_log: Optional[LogFn] = None,
        on_progress: Optional[ProgressFn] = None,
        on_result: Optional[ResultFn] = None,
        run_dir: Optional[Path] = None,
    ) -> None:
        self.on_log = on_log
        self.on_progress = on_progress
        self.on_result = on_result
        self.results: list[ActionResult] = []
        self.undo: list[dict] = []
        self.run_dir = run_dir
        self._csv_path: Optional[Path] = None
        if run_dir is not None:
            run_dir.mkdir(parents=True, exist_ok=True)
            self._csv_path = run_dir / "results.csv"
            with open(self._csv_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(list(ActionResult.__dataclass_fields__.keys()))

    # -- logging -----------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        if self.on_log:
            self.on_log(level, message)

    def info(self, message: str) -> None:
        self.log(message, "info")

    def warn(self, message: str) -> None:
        self.log(message, "warn")

    def error(self, message: str) -> None:
        self.log(message, "error")

    def progress(self, done: int, total: int, label: str = "") -> None:
        if self.on_progress:
            self.on_progress(done, total, label)

    # -- results -----------------------------------------------------------
    def result(self, r: ActionResult) -> None:
        self.results.append(r)
        if self._csv_path is not None:
            with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(list(asdict(r).values()))
        if self.on_result:
            self.on_result(r)

    def record_undo(self, entry: dict) -> None:
        """Remember a reversible change (e.g. an agenda we unpublished) so a
        later run can put it back. Written immediately, not at the end."""
        self.undo.append(entry)
        if self.run_dir is not None:
            (self.run_dir / "undo.json").write_text(json.dumps(self.undo, indent=2), encoding="utf-8")

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


def new_run_dir(base: Path, live: bool) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return base / f"{stamp}_{'live' if live else 'dryrun'}"
