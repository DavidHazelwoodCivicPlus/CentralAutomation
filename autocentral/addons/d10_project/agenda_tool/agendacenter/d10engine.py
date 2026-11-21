"""Run the D10-to-Central Migration Tool (the team's HTML page) without
clicking through it, and keep what its Agenda Center step produces.

The page is opened in a hidden Chrome. The old site's JSON exports are loaded
the way Step 1 does it (its own file box), the site time zone is picked in
its own list, and then its own Agenda Center download buttons are clicked.
Every download is caught before it reaches the Downloads folder:

  <site>-Master_Content_Import.xlsx            the import workbook
  <site>-agenda-center-download-files.txt      PowerShell script: every file's
                                               URL, its name in the zip, and
                                               whether it goes in the zip or
                                               the attach-after-import folder
  <site>-agenda-center-post-import-tasks.json  the to-do list's input
  <site>-agenda-center-categories.csv
  <site>-agenda-center-flagged-for-manual-review.csv
  No-Agenda-Template.pdf                       the minutes-only template agenda

So the rules (categories from microsites, item-based meetings, same-date
collisions, likely duplicates, renamed files, withheld minutes, …) are
exactly the page's own, for whichever build of the tool is picked.
"""

from __future__ import annotations

import base64
import csv
import io
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ZONES = {  # the page's own time zone list
    "Eastern": "America/New_York", "Central": "America/Chicago", "Mountain": "America/Denver",
    "Arizona": "America/Phoenix", "Pacific": "America/Los_Angeles", "Alaska": "America/Anchorage",
    "Hawaii": "Pacific/Honolulu",
}
BUTTONS = ["acDownloadImportBtn", "acDownloadScriptBtn", "acDownloadTasksJsonBtn", "acDownloadCategoriesBtn",
           "acDownloadFlaggedBtn", "acDownloadTemplateBtn"]
IN_ZIP, ATTACH = "In import zip", "Attach after import"

_CAPTURE_JS = r"""
window.__caps = [];
URL.revokeObjectURL = function () {};
const toB64 = buf => { let s = ''; const b = new Uint8Array(buf);
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return btoa(s); };
HTMLAnchorElement.prototype.click = function () {
  const name = this.download, href = this.href || '';
  if (name && href.startsWith('blob:')) {
    window.__caps.push(fetch(href).then(r => r.arrayBuffer()).then(b => ({ name, b64: toB64(b) })));
  }
};
"""

_CLICK_JS = r"""
const done = arguments[arguments.length - 1];
const ids = arguments[0], errors = [];
ids.forEach(id => {
  const b = document.getElementById(id);
  if (!b) { errors.push(id + ' not on the page'); return; }
  b.disabled = false;
  try { b.click(); } catch (e) { errors.push(id + ': ' + e); }
});
setTimeout(() => Promise.all(window.__caps).then(files => {
  const msg = document.getElementById('acActionMessage');
  done({ files, errors, message: msg ? msg.textContent : '' });
}).catch(e => done({ files: [], errors: errors.concat(String(e)), message: '' })), 500);
"""


@dataclass
class FileToGet:
    url: str
    name: str            # name in the zip / folder (the tool renames clashing names: x-1.pdf)
    dest: str            # IN_ZIP | ATTACH
    meeting: str = ""


@dataclass
class EngineResult:
    site: str                                   # the tool's slug, e.g. yorksc
    folder: Path
    files: dict[str, Path] = field(default_factory=dict)      # output name -> saved path
    to_get: list[FileToGet] = field(default_factory=list)
    unpublished: list[dict] = field(default_factory=list)     # files the export marks unpublished
    workbook: Optional[Path] = None
    tasks_json: Optional[Path] = None
    categories: list[str] = field(default_factory=list)
    summary: str = ""


def newest_tool(folder: Path) -> Optional[Path]:
    hits = sorted(Path(folder).glob("D10-to-Central-Migration-Tool*.htm*"), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def export_files(folder: Path) -> list[Path]:
    """The D10 JSON exports in a folder (www-meetings.json, www-media.json, …)."""
    return sorted(p for p in Path(folder).glob("*.json")
                  if not re.search(r"post-import-tasks|uploaded|settings", p.name, re.I))


def _ps_strings(line: str) -> list[str]:
    return [m.replace("''", "'") for m in re.findall(r"'((?:[^']|'')*)'", line)]


def parse_script(text: str) -> tuple[list[FileToGet], list[dict]]:
    """The tool's PowerShell script -> the files to download (and the
    unpublished ones it only logs)."""
    gets, unpublished = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Get-AgendaFile "):
            parts = _ps_strings(s)
            if len(parts) >= 3:
                gets.append(FileToGet(parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else ""))
        elif s.startswith("Add-LogEntry 'Omitted - unpublished'"):
            parts = _ps_strings(s)
            if len(parts) >= 3:
                unpublished.append({"file": parts[1], "url": parts[2], "meeting": parts[-1]})
    return gets, unpublished


def run_tool(tool_html: Path, exports: list[Path], zone: str, out_dir: Path,
             log: Callable[[str], None] = lambda m: None, headless: bool = True) -> EngineResult:
    """Open the migration tool, load the exports, pick the time zone, click
    the Agenda Center downloads and save what they produce in out_dir."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By

    tz = ZONES.get(zone, zone)
    if tz not in ZONES.values():
        raise ValueError(f"Unknown time zone {zone!r}.")
    if not any("meetings" in p.name.lower() for p in exports):
        raise ValueError("No meetings export (www-meetings.json) among the files.")
    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--allow-file-access-from-files")
    d = webdriver.Chrome(options=opts)
    try:
        d.set_script_timeout(600)
        log(f"Opening {tool_html.name}…")
        d.get(Path(tool_html).resolve().as_uri())
        box = d.find_element(By.ID, "fileInput")
        box.send_keys("\n".join(str(p.resolve()) for p in exports))
        t0 = time.monotonic()
        while True:
            n = d.execute_script("return (typeof contentStore !== 'undefined' && contentStore.meetings) ? "
                                 "contentStore.meetings.records.length : -1;")
            loaded = d.execute_script("return typeof contentStore !== 'undefined' ? Object.keys(contentStore).length : 0;")
            if n >= 0 and loaded >= len(exports):
                break
            if time.monotonic() - t0 > 300:
                raise RuntimeError("The migration tool didn't finish loading the exports in 5 minutes.")
            time.sleep(0.5)
        log(f"Loaded {len(exports)} export(s); {n} meeting record(s).")
        d.execute_script("const s = document.getElementById('siteTimezoneSelect'); s.value = arguments[0];"
                         "s.dispatchEvent(new Event('change', {bubbles: true}));", tz)
        if d.execute_script("return window.getSiteTimezone();") != tz:
            raise RuntimeError("Couldn't set the time zone in the migration tool.")
        d.execute_script(_CAPTURE_JS)
        res = d.execute_async_script(_CLICK_JS, BUTTONS)
    finally:
        d.quit()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = EngineResult("", out_dir)
    for f in res["files"]:
        data = base64.b64decode(f["b64"])
        p = out_dir / f["name"]
        p.write_bytes(data)
        result.files[f["name"]] = p
    for e in res["errors"]:
        log("Migration tool: " + e)
    wb = [p for n, p in result.files.items() if n.endswith("Master_Content_Import.xlsx")]
    if not wb:
        raise RuntimeError("The migration tool made no import workbook. "
                           + (res.get("message") or "Is www-meetings.json among the exports?"))
    result.workbook = wb[0]
    result.site = wb[0].name[: -len("-Master_Content_Import.xlsx")]
    for n, p in result.files.items():
        if n.endswith("download-files.txt"):
            result.to_get, result.unpublished = parse_script(p.read_text(encoding="utf-8-sig"))
        elif n.endswith("post-import-tasks.json"):
            result.tasks_json = p
        elif n.endswith("agenda-center-categories.csv"):
            rows = list(csv.reader(io.StringIO(p.read_text(encoding="utf-8-sig"))))
            result.categories = [r[0] for r in rows[1:] if r and r[0].strip()]
    n_zip = sum(1 for g in result.to_get if g.dest == IN_ZIP)
    result.summary = (f"{result.site}: workbook, {len(result.categories)} categories, {n_zip} file(s) for the "
                      f"import, {len(result.to_get) - n_zip} to attach after import"
                      + (f", {len(result.unpublished)} unpublished file(s) left out" if result.unpublished else "")
                      + (", post-import tasks" if result.tasks_json else ", no post-import tasks") + ".")
    if res.get("message"):
        log("Migration tool says: " + res["message"].strip())
    return result
