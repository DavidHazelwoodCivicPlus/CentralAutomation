"""The Content Import workbooks and zips.

Starts from the site's own template (Master_Content Import.xlsx, next to this
file), with the two column names Content Import actually reads (checked on
training19, 2026-11-20): "Parent Folder Name" (the template says "Names") and
a Pages column "MainContentContainerWidgets" = "Editor".

Two imports, in this order:
  1. files    Document Center and Public Images: every file and picture,
              under the project's own top folders (made first, by name, so
              they're unique: Content Import finds a parent folder by name).
  2. content  Pages (parents first: the sheet is read top to bottom), News
              Flash, Jobs, Staff Directory and Archive Center, with every
              address already pointing at the new files.
Each zip stays under the size limit (Content Import takes 1 GB at most).
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

TEMPLATE = Path(__file__).with_name("Master_Content Import.xlsx")
WORKBOOK_NAME = "Master_Content Import.xlsx"
DEFAULT_LIMIT_MB = 900
staff_photos_in_import = False      # see write_content_zip
HEADER_FIXES = {"DocumentCenter": {3: "Parent Folder Name"}, "ImageRepository": {3: "Parent Folder Name"},
                "Pages": {5: "MainContentContainerWidgets"},
                "StaffDirectory": {11: "documentFolderName"}}       # the template says "docuemntFolderName"


def long_path(p) -> Path:
    from .agenda_tool.agendacenter.batchzip import long_path as lp
    return lp(p)


def blank():
    """The template with its sample rows gone and the column names fixed."""
    import openpyxl
    wb = openpyxl.load_workbook(TEMPLATE)
    for ws in wb.worksheets:
        if ws.max_row > 1:
            ws.delete_rows(2, ws.max_row)
    for sheet, cols in HEADER_FIXES.items():
        for col, name in cols.items():
            wb[sheet].cell(1, col).value = name
    return wb


def _xl(s) -> str:
    """Excel cells hold 32,767 characters at most, and no control characters."""
    s = "" if s is None else str(s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s[:32000]


def _bytes(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _day(s: str) -> Optional[date]:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ============================================================ 1. files
def file_rows(files: list[dict]) -> list[dict]:
    """The downloaded files that go to Document Center / Public Images."""
    return [f for f in files if f["download"] in ("ok", "skipped") and f["root"] != "Archive Center"
            and f["imported"] != "yes"]


def write_files_batches(files: list[dict], files_dir: Path, out_dir: Path, site: str,
                        limit_mb: int = DEFAULT_LIMIT_MB, log: Callable[[str], None] = lambda m: None) -> list[Path]:
    """Zips of the files, each with its own workbook rows. Returns the zips."""
    out_dir.mkdir(parents=True, exist_ok=True)
    limit = limit_mb * 2**20
    batches: list[list[dict]] = [[]]
    size = 0
    for f in file_rows(files):
        n = f.get("size") or 0
        if batches[-1] and size + n > limit:
            batches.append([])
            size = 0
        batches[-1].append(f)
        size += n
    zips = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i, batch in enumerate(b for b in batches if b):
        wb = blank()
        for f in batch:
            sheet = "ImageRepository" if f["image"] else "DocumentCenter"
            wb[sheet].append([f["file"], f["folder"], f["root"], _xl(f["alt"] if f["image"] else ""),
                              _xl(f["title"]), ""])
        z = out_dir / f"{site}-files-{stamp}-{i + 1:02d}.zip"
        with zipfile.ZipFile(long_path(z), "w", zipfile.ZIP_DEFLATED) as zo:
            zo.writestr(WORKBOOK_NAME, _bytes(wb))
            for f in batch:
                zo.write(long_path(files_dir / f["file"]), f["file"], compress_type=zipfile.ZIP_STORED)
        log(f"{z.name}: {len(batch)} file(s), {sum(f.get('size') or 0 for f in batch) / 2**20:.0f} MB")
        zips.append(z)
    return zips


# ============================================================ 2. content
def page_rows(pages: list[dict]) -> list[list]:
    out = []
    by = {p["key"]: p for p in pages}
    for p in pages:
        if p["state"] not in ("planned", "failed"):
            continue
        parent = by[p["parent"]]["title"] if p["parent"] in by else p["parent"]     # "Home" or a page
        html = p.get("html_new", p["html"]) or ""
        if p["kind"] == "linked" and not html:
            html = f'<p><a href="{p["link_to"]}">{p["title"]}</a></p>'
        out.append([_xl(p["title"]), "", _xl(parent), _xl(html), "Editor"])
    return out


def write_content_zip(pr, out_dir: Path, files_dir: Path, limit_mb: int = DEFAULT_LIMIT_MB,
                      log: Callable[[str], None] = lambda m: None) -> list[Path]:
    """The content import: pages, news, jobs, staff, archive. Archive files
    travel in the zip (Content Import attaches them by name); a second zip is
    made only when they don't fit in one."""
    out_dir.mkdir(parents=True, exist_ok=True)
    wb = blank()
    for row in page_rows(pr.pages):
        wb["Pages"].append(row)
    for n in pr.news:
        if n["state"] not in ("planned", "failed"):
            continue
        d = _day(n["date"]) or date.today()
        wb["NewsFlash"].append([_xl(n["category"]), d, None, d + timedelta(days=30), None, "no", _xl(n["title"]),
                                None, None, None, None, None, _xl(n.get("html_new", n["html"])), None, None, None,
                                None, "no", None, "yes", "yes"])
    for j in pr.jobs:
        if j["state"] not in ("planned", "failed"):
            continue
        start = _day(j.get("start", "")) or date.today()          # Content Import requires a Start Date
        wb["Jobs"].append([_xl(j["category"]), _xl(j["title"]), _xl(j.get("html_new", j["html"])), None, start])
    staff_photos = []
    for s in pr.staff:
        if s["state"] not in ("planned", "failed"):
            continue
        # The photo travels in the zip, and the folder is the project's own
        # (uniquely named) picture folder: a photo that's only named, or whose
        # folder name isn't unique, makes the person silently not import.
        photo = pr.file(s["photo"]) if s["photo"] else None
        # Photos through Content Import proved unreliable (people silently left
        # out); they're added afterwards in the Staff Directory instead.
        ok_photo = staff_photos_in_import and photo and photo["download"] in ("ok", "skipped")
        if ok_photo:
            staff_photos.append(photo)
        wb["StaffDirectory"].append([_xl(s["first"]), _xl(s["last"]), _xl(s["title"]), _xl(s["phone"]), _xl(s["email"]),
                                     _xl(f"Email {s['first']} {s['last']}".strip()) if s["email"] else "",
                                     _xl(s["bio"]), _xl(s["department"]), "",
                                     photo["file"] if ok_photo else "", pr.options["images_root"] if ok_photo else "",
                                     _xl(photo["alt"]) if ok_photo else ""])
    archive_files = []
    for a in pr.archive:
        if a["state"] not in ("planned", "failed"):
            continue
        f = next((pr.file(m) for m in a["media"] if pr.file(m) and pr.file(m)["download"] in ("ok", "skipped")), None)
        if f is None:
            a["state"], a["detail"] = "left out", "its file couldn't be downloaded"
            continue
        d = _day(a["date"])
        wb["ArchiveCenter"].append([_xl(a["category"]), _xl(a["title"]), d, _xl(a["description"]), d, None,
                                    "no", "yes", f["file"], "no", None])
        archive_files.append(f)
    z = out_dir / f"{pr.site}-content-{datetime.now():%Y%m%d-%H%M%S}.zip"
    with zipfile.ZipFile(long_path(z), "w", zipfile.ZIP_DEFLATED) as zo:
        zo.writestr(WORKBOOK_NAME, _bytes(wb))
        seen = set()
        for f in archive_files + staff_photos:
            if f["file"] not in seen:
                seen.add(f["file"])
                zo.write(long_path(files_dir / f["file"]), f["file"], compress_type=zipfile.ZIP_STORED)
    mb = z.stat().st_size / 2**20
    if mb > limit_mb:
        raise ValueError(f"The content zip is {mb:.0f} MB, over the {limit_mb} MB limit (too many Archive Center files).")
    log(f"{z.name}: {wb['Pages'].max_row - 1} page(s), {wb['NewsFlash'].max_row - 1} news, {wb['Jobs'].max_row - 1} job(s), "
        f"{wb['StaffDirectory'].max_row - 1} staff, {wb['ArchiveCenter'].max_row - 1} archive item(s), {mb:.0f} MB")
    return [z]


# ============================================================ module imports
CAL_HEADER = ["*Calendar Name", "*Event Title", "Event ID", "*Start Date", "Start Time", "End Date", "End Time", "Location",
              "Address 1", "Address 2", "City", "State", "Zip Code", "Link to Map", "Open map in new window",
              "Time Details", "Cost", "Description", "Link (web address)", "Link Text", "Open link in new window",
              "Agenda Link", "Open agenda link in new window", "Phone - Number", "Email Address", "Show Email As",
              "Featured Event", "ADA option", "Event Details", "Display Start Date", "Display Stop Date",
              "Show In Widget", "Related Calendars", "Recurrence Pattern"]


def calendar_csv(events: list[dict], path: Path) -> Path:
    """The Calendar module's own import file (Calendar > Import)."""
    import csv
    from datetime import datetime as dt
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(CAL_HEADER)
        for e in events:
            if e["state"] not in ("planned", "failed"):
                continue
            s = dt.fromtimestamp(int(e["start"]))
            en = dt.fromtimestamp(int(e["end"] or e["start"]))
            all_day = s.hour == 0 and s.minute == 0 and (en - s).total_seconds() >= 86000
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("html_new", e["html"]) or "")).strip()
            w.writerow([e["calendar"], e["title"], "", f"{s:%m/%d/%Y}", "" if all_day else f"{s:%I:%M %p}",
                        f"{en:%m/%d/%Y}", "" if all_day else f"{en:%I:%M %p}", "", "", "", "", "", "", "0", "0",
                        "All day" if all_day else "", "", text[:4000], "", "", "0", "", "0", "", "", "", "0", "N/A",
                        e.get("html_new", e["html"]) or "", f"{s:%m/%d/%Y}", "", "1", "", ""])
    return path
