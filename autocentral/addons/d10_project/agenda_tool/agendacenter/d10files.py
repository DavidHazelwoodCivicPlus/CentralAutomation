"""Download the meeting files from the old (D10) site, the way the migration
tool's PowerShell script does, but in parallel and safe to run again.

Each file is checked the same way: 404/410 means it's gone from the old site
("Omitted - not found"); a web page where a file was expected (a login or
error page), an empty file, or a ".pdf" that isn't a PDF is "Failed". A file
already in the folder is not fetched again, so a stopped run picks up where
it left off. Every problem goes to <site>-agenda-center-file-log.csv.
"""

from __future__ import annotations

import csv
import os
import shutil
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .batchzip import long_path
from .d10engine import ATTACH, FileToGet

HTML_EXT = {".htm", ".html", ".xhtml", ".shtml", ".php", ".asp", ".aspx"}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CentralAutomation file copy"


@dataclass
class Outcome:
    file: FileToGet
    result: str          # ok | skipped (already there) | Omitted - not found | Failed
    reason: str = ""
    http: str = ""


def _check(path: Path, name: str, ctype: str) -> Optional[str]:
    if not path.is_file() or path.stat().st_size == 0:
        return "The server returned an empty file"
    with open(path, "rb") as f:
        head = f.read(1024).decode("ascii", "replace")
    ext = os.path.splitext(name)[1].lower()
    looks_html = ctype.lower().startswith(("text/html", "application/xhtml")) or \
        head.lstrip()[:15].lower().startswith(("<!doctype html", "<html"))
    if ext not in HTML_EXT and looks_html:
        return f"The server returned a web page instead of the file (likely a login or error page; content type '{ctype}')"
    if ext == ".pdf" and "%PDF" not in head:
        return "The server returned something other than a PDF"
    return None


def fetch_one(f: FileToGet, folder: Path, timeout: float = 900) -> Outcome:
    out = long_path(folder / f.name)      # some names make the full path longer than Windows' 260 characters
    if out.is_file() and out.stat().st_size > 0:
        return Outcome(f, "skipped")
    part = out.with_name(out.name + ".part")
    req = urllib.request.Request(f.url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as w:
            shutil.copyfileobj(r, w, 1 << 20)
            ctype, code = r.headers.get("Content-Type", ""), str(r.status)
    except urllib.error.HTTPError as e:
        part.unlink(missing_ok=True)
        if e.code in (404, 410):
            return Outcome(f, "Omitted - not found",
                           f"Not on the source site (HTTP {e.code}): the file was removed or is no longer published",
                           str(e.code))
        if e.code in (401, 403):
            return Outcome(f, "Failed", f"Access denied (HTTP {e.code}): probably behind a login. Download it by "
                                        "hand while signed in", str(e.code))
        return Outcome(f, "Failed", f"HTTP error {e.code}", str(e.code))
    except Exception as e:      # timeouts, DNS, dropped connections
        part.unlink(missing_ok=True)
        return Outcome(f, "Failed", f"No response from the server: {e}")
    problem = _check(part, f.name, ctype)
    if problem:
        part.unlink(missing_ok=True)
        return Outcome(f, "Failed", problem, code)
    os.replace(part, out)
    return Outcome(f, "ok", http=code)


def download_all(files: list[FileToGet], import_dir: Path, attach_dir: Path, log_csv: Path,
                 unpublished: Optional[list[dict]] = None, workers: int = 6,
                 log: Callable[[str], None] = lambda m: None,
                 cancelled: Callable[[], bool] = lambda: False) -> list[Outcome]:
    import_dir.mkdir(parents=True, exist_ok=True)
    if any(f.dest == ATTACH for f in files):
        attach_dir.mkdir(parents=True, exist_ok=True)
    outcomes: list[Outcome] = []
    total = len(files)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_one, f, attach_dir if f.dest == ATTACH else import_dir): f for f in files}
        for n, fut in enumerate(as_completed(futs), 1):
            o = fut.result()
            outcomes.append(o)
            if o.result not in ("ok", "skipped"):
                log(f"[{n}/{total}] {o.file.name}: {o.result.upper()}: {o.reason}")
            elif n % 25 == 0 or n == total:
                log(f"[{n}/{total}] files done.")
            if cancelled():
                for other in futs:
                    other.cancel()
                raise InterruptedError("Stopped; files already downloaded are kept.")
    problems = [o for o in outcomes if o.result not in ("ok", "skipped")]
    with open(log_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Result", "File", "Reason", "HttpStatus", "Destination", "Meeting", "SourceUrl"])
        for o in problems:
            w.writerow([o.result, o.file.name, o.reason, o.http, o.file.dest, o.file.meeting, o.file.url])
        for u in unpublished or []:
            w.writerow(["Omitted - unpublished", u["file"], "Marked unpublished on the source site, so it was not "
                        "downloaded", "", "Not downloaded", u.get("meeting", ""), u["url"]])
    return outcomes
