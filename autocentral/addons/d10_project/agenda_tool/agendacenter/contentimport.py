"""Upload zips through the site's own Content Import (/Admin/ContentImport).

The page has one file box (#uploadedContentZip) and Start Import. The POST
runs the whole import before the answer page loads, which takes minutes for
a big zip, then shows "Imported N rows in S seconds" with "Successful
imports:" and "Failed imports:" lists.

Content Import isn't safe to repeat: the same zip imported twice makes every
agenda twice. So each upload is written to <batch folder>/uploaded.json
before it starts and again when it ends, per site, and a zip already
uploaded (or whose upload was cut off) to that site is never sent again.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from selenium.webdriver.common.by import By

PAGE = "/Admin/ContentImport"
DONE_RE = re.compile(r"Imported\s+(\d+)\s+rows?\s+in\s+([\d.]+)\s+seconds", re.I)
MAX_WAIT = 3 * 3600


@dataclass
class ImportReport:
    rows: int = 0
    seconds: float = 0
    succeeded: Counter = field(default_factory=Counter)    # module -> count
    failed: Counter = field(default_factory=Counter)       # "module: reason" -> count
    text: str = ""

    def describe(self) -> str:
        out = (f"{self.rows} row(s) in {self.seconds:.0f}s: {self.succeeded['agendas']} agenda(s) and "
               f"{self.succeeded['items']} item(s) imported")
        if self.failed:
            out += f", {sum(self.failed.values())} failed (" + "; ".join(
                f"{n} x {k}" for k, n in self.failed.most_common(6)) + ")"
        return out


def parse_report(text: str) -> Optional[ImportReport]:
    m = DONE_RE.search(text or "")
    if not m:
        return None
    rep = ImportReport(int(m.group(1)), float(m.group(2)), text=text)
    section = None
    for line in text[m.end():].splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("successful imports"):
            section = "ok"
        elif low.startswith("failed imports"):
            section = "failed"
        elif not s or section is None:
            continue
        elif section == "failed" or "failed import" in low:
            # "Agenda Center failed import: Meeting Date is empty." or
            # "Agenda Center started importing item: X | Agenda Center Item failed import: X. <reason>"
            what = "item" if "item" in low.split("failed import")[0] else "agenda"
            why = s.rsplit("failed import:", 1)[-1].strip()
            if what == "item":
                why = why.split(". ", 1)[-1] if ". " in why else why
            rep.failed[f"{what}: {why}"] += 1
        elif "importing item" in low:
            rep.succeeded["items"] += 1
        else:
            rep.succeeded["agendas"] += 1
    return rep


class UploadLog:
    """<batch folder>/uploaded.json: which zips went to which site."""

    def __init__(self, folder: Path) -> None:
        self.path = Path(folder) / "uploaded.json"
        try:
            self.entries: list[dict] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.entries = []

    def find(self, host: str, zip_path: Path) -> Optional[dict]:
        for e in self.entries:
            if e.get("site") == host and e.get("zip") == zip_path.name:
                return e
        return None

    def record(self, host: str, zip_path: Path, state: str, detail: str = "") -> None:
        e = self.find(host, zip_path)
        if e is None:
            e = {"site": host, "zip": zip_path.name}
            self.entries.append(e)
        e.update(state=state, detail=detail, size=zip_path.stat().st_size,
                 time=datetime.now().isoformat(timespec="seconds"))
        self._write()

    def forget(self, host: str, zip_path: Path) -> None:
        self.entries = [e for e in self.entries if not (e.get("site") == host and e.get("zip") == zip_path.name)]
        self._write()

    def _write(self) -> None:
        self.path.write_text(json.dumps(self.entries, indent=2), encoding="utf-8")


STALL_AFTER = 600          # seconds with no new agenda on the site before an import counts as stuck


class ImportStalled(Exception):
    """Nothing new reached the site for STALL_AFTER seconds."""


def zip_categories(zip_path: Path) -> list[str]:
    """The categories a batch zip's workbook uses."""
    import io
    import zipfile
    import openpyxl
    from .importdata import _read_sheet, _s
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".xlsx"))
        wb = openpyxl.load_workbook(io.BytesIO(z.read(name)), read_only=True, data_only=True)
    try:
        rows = _read_sheet(wb, "AgendaCenter") + _read_sheet(wb, "AgendaCenterItems")
    finally:
        wb.close()
    return sorted({_s(r.get("Category")) for r in rows if _s(r.get("Category"))}, key=str.lower)


def zip_units(zip_path: Path) -> tuple[set, dict]:
    """The (category, meeting date) meetings in a batch zip, and how many
    agenda rows each category has."""
    import io
    import zipfile
    from collections import Counter
    import openpyxl
    from .importdata import _read_sheet, _s, _to_date
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".xlsx"))
        wb = openpyxl.load_workbook(io.BytesIO(z.read(name)), read_only=True, data_only=True)
    try:
        agendas = _read_sheet(wb, "AgendaCenter")
        rows = agendas + _read_sheet(wb, "AgendaCenterItems")
    finally:
        wb.close()
    units = {(_s(r.get("Category")).lower(), _to_date(r.get("Meeting Date"))) for r in rows if _s(r.get("Category"))}
    return units, dict(Counter(_s(r.get("Category")).lower() for r in agendas if _s(r.get("Category"))))


def upload_zip(b, zip_path: Path, max_wait: float = MAX_WAIT, on_submit: Optional[Callable[[], None]] = None,
               progress: Optional[Callable[[], int]] = None, stall_after: float = STALL_AFTER,
               check_every: float = 60) -> ImportReport:
    """Send one zip through Content Import and wait for its report.
    progress: how many agendas the batch's categories have now; when it
    doesn't change for stall_after seconds, ImportStalled is raised."""
    from .site import SiteError
    def send() -> None:
        b.get(PAGE)
        box = b.wait_css("#uploadedContentZip", 30)
        box.send_keys(str(Path(zip_path).resolve()))
        start = b.d.find_element(By.CSS_SELECTOR, "input[type=submit][value='Start Import'], button[type=submit]")
        # Click from a timer inside the page, so the click returns at once. A
        # plain click waits for the report page, i.e. the whole import, and
        # Selenium gives up on any one command after 120 s (ReadTimeoutError),
        # while Chrome carries on importing.
        b.d.execute_script("var el = arguments[0]; setTimeout(function () { el.click(); }, 100);", start)

    def idle_form() -> bool:
        """Still the plain Content Import form with no file chosen and no
        report: the upload never started (seen live 2026-11-19)."""
        try:
            return bool(b.d.execute_script(
                "var f = document.getElementById('uploadedContentZip');"
                "return !!f && !f.value && !/Imported\s+\d+\s+rows/i.test(document.body.innerText);"))
        except Exception:
            return False                # mid-navigation: it's going

    if on_submit is not None:
        on_submit()                     # from here on, part of the zip may reach the site
    t0 = time.monotonic()
    for attempt in range(3):
        send()
        time.sleep(15)
        if not idle_form():
            break
        time.sleep(5)
        if not idle_form():
            break
        if attempt == 2:
            raise SiteError("Content Import didn't start the upload after 3 tries (the file box stayed empty).")
    last_n, last_change, last_check = None, time.monotonic(), time.monotonic()
    while True:
        text = ""
        try:
            text = b.d.execute_script("return document.body ? document.body.innerText : ''") or ""
        except Exception:               # the page is mid-load, or a command timed out: ask again
            pass
        rep = parse_report(text)
        if rep is not None:
            return rep
        try:
            logged_out = b.on_login_page()
        except Exception:
            logged_out = False
        if logged_out:
            raise SiteError("The site logged you out during the import. Log in again, then check the "
                            "Agenda Center before importing this zip again: part of it may be in.")
        if time.monotonic() - t0 > max_wait:
            raise SiteError(f"No import report after {max_wait / 3600:.0f} hours.")
        if progress is not None and time.monotonic() - last_check >= check_every:
            last_check = time.monotonic()
            try:
                n = progress()
            except Exception:
                n = last_n
            if n != last_n:
                last_n, last_change = n, time.monotonic()
            elif time.monotonic() - last_change > stall_after:
                raise ImportStalled(f"nothing new on the site for {stall_after / 60:.0f} minutes")
        time.sleep(2)
