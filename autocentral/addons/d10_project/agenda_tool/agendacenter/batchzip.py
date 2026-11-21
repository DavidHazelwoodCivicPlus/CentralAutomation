"""Turn the migration tool's Agenda Center import zip into zips Content
Import takes.

The zip (<site>-agenda-center-import.zip) holds <site>-Master_Content_Import.xlsx
and every file its rows name. Two things can stop it importing:

  * Size. Content Import takes one zip of up to 1 GB per upload.
  * Missing meeting dates. Central refuses a row with no Meeting Date, and
    the migration tool sometimes writes none at all (yorksc, cityofhiawatha).
    The old site's export (www-meetings.json) has them, so they're filled in
    from there: the same matching the Compare uses (importdata._apply_d10).

plan_batches() reads the zip and works out the batches; write_batches()
writes them. Each batch is a zip with the same workbook name, holding only
its own rows (dates filled in) and the files those rows name. A meeting's
agenda row and its item rows (same category and date) always go in the same
batch, so Central builds each meeting in one go. Batches run in date order.

Rows that would only fail (no category, no date to be found, a file that
isn't in the zip) are left out and listed in left-out.csv, so the import
report only shows real problems. Upload missing… or the to-do list can add
those afterwards.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from .importdata import ImportOptions, _apply_d10, _read_sheet, _s, _to_date

AGENDA_SHEET = "AgendaCenter"
ITEMS_SHEET = "AgendaCenterItems"
UPLOAD_LIMIT = 1_000_000_000          # Content Import: 1 GB per upload
DEFAULT_LIMIT_MB = 900                 # headroom under the limit
ITEM_FILE_COLS = [f"File Name {i}" for i in range(1, 21)]


@dataclass
class LeftOut:
    sheet: str
    row: int
    category: str
    description: str
    reason: str


@dataclass
class Unit:
    """One meeting: its agenda row(s) and item rows, which share a category and date."""
    category: str
    meeting_date: Optional[date]          # None: the category's default items
    agenda_rows: list[int] = field(default_factory=list)
    item_rows: list[int] = field(default_factory=list)
    files: set[str] = field(default_factory=set)


@dataclass
class Batch:
    number: int
    units: list[Unit] = field(default_factory=list)
    files: set[str] = field(default_factory=set)
    size: int = 0                         # bytes of the files, as stored in the source zip
    path: Optional[Path] = None

    @property
    def agendas(self) -> int:
        return sum(len(u.agenda_rows) for u in self.units)

    @property
    def items(self) -> int:
        return sum(len(u.item_rows) for u in self.units)

    def date_range(self) -> str:
        ds = sorted(u.meeting_date for u in self.units if u.meeting_date)
        if not ds:
            return "default items"
        return f"{ds[0]:%m/%d/%Y} to {ds[-1]:%m/%d/%Y}" if ds[0] != ds[-1] else f"{ds[0]:%m/%d/%Y}"

    def describe(self) -> str:
        return (f"batch {self.number:02d}: {self.size / 2**20:,.0f} MB, {self.agendas} agenda row(s), "
                f"{self.items} item row(s), {len(self.files)} file(s), meetings {self.date_range()}")


class ZipSource:
    """The migration tool's import zip: workbook and files together."""

    def __init__(self, zip_path: Path) -> None:
        self.path = Path(zip_path)
        with zipfile.ZipFile(self.path) as z:
            self.workbook_name = _workbook_in(z)
            self.entries = {i.filename.split("/")[-1].lower(): i for i in z.infolist()
                            if not i.is_dir() and i.filename != self.workbook_name}
        self.sizes = {n: i.compress_size for n, i in self.entries.items()}

    def workbook_bytes(self) -> bytes:
        with zipfile.ZipFile(self.path) as z:
            return z.read(self.workbook_name)

    def copy_into(self, zo: zipfile.ZipFile, names: set[str]) -> None:
        with zipfile.ZipFile(self.path) as z:
            for name in sorted(names):
                info = self.entries[name]
                ni = zipfile.ZipInfo(info.filename.split("/")[-1], info.date_time)
                ni.compress_type = info.compress_type
                ni.file_size = info.file_size
                with z.open(info) as src, zo.open(ni, "w", force_zip64=info.file_size > 2**31) as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)


def long_path(p) -> Path:
    r"""p in Windows' long-path form (\\?\C:\...), so files whose full path
    is over 260 characters can still be written, read and listed. Unchanged
    elsewhere and for paths already in that form."""
    s = str(p)
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return Path(s)
    s = os.path.abspath(s)
    return Path("\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s)


class FolderSource:
    """A workbook plus the folder(s) its files were downloaded to."""

    def __init__(self, workbook: Path, folders: list[Path]) -> None:
        self.path = Path(workbook)
        self.workbook_name = self.path.name
        self.files: dict[str, Path] = {}
        for folder in folders:
            folder = long_path(folder)
            for f in sorted(folder.iterdir()) if folder.is_dir() else []:
                if f.is_file() and not f.name.endswith(".part") and f.name != self.path.name:
                    self.files.setdefault(f.name.lower(), f)
        self.sizes = {n: f.stat().st_size for n, f in self.files.items()}

    def workbook_bytes(self) -> bytes:
        return self.path.read_bytes()

    def copy_into(self, zo: zipfile.ZipFile, names: set[str]) -> None:
        for name in sorted(names):
            f = self.files[name]
            # PDFs barely compress; storing them is much faster and the size is known up front.
            ctype = zipfile.ZIP_STORED if f.suffix.lower() in (".pdf", ".jpg", ".jpeg", ".png", ".zip", ".mp4",
                                                                ".docx", ".xlsx", ".pptx") else zipfile.ZIP_DEFLATED
            zo.write(f, f.name, compress_type=ctype)


@dataclass
class BatchPlan:
    zip_path: Path                         # the source: the import zip, or the workbook of a folder source
    workbook_name: str
    site: str
    limit: int
    batches: list[Batch] = field(default_factory=list)
    left_out: list[LeftOut] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dates_filled: int = 0
    agenda_total: int = 0
    item_total: int = 0
    other_sheets: dict[str, int] = field(default_factory=dict)     # sheet -> rows (other modules)
    unreferenced: list[str] = field(default_factory=list)          # files no agenda row names
    dates: dict[tuple[str, int], date] = field(default_factory=dict)   # (sheet, row) -> meeting date
    source: object = None

    def summary(self) -> str:
        lines = [f"{self.zip_path.name}: {self.agenda_total} agenda row(s), {self.item_total} item row(s)."]
        if self.dates_filled:
            lines.append(f"Meeting dates filled in from the old site's export: {self.dates_filled} row(s).")
        lines += [b.describe() for b in self.batches]
        if self.left_out:
            reasons: dict[str, int] = {}
            for x in self.left_out:
                key = re.sub(r":.*", "", x.reason)
                reasons[key] = reasons.get(key, 0) + 1
            lines.append(f"Left out (listed in left-out.csv): {len(self.left_out)} row(s): "
                         + "; ".join(f"{n} {k}" for k, n in sorted(reasons.items(), key=lambda kv: -kv[1])))
        lines += self.warnings
        return "\n".join(lines)


def site_name(zip_path: Path, workbook_name: str = "") -> str:
    for text, sep in ((workbook_name, "-master_content_import"), (zip_path.stem, "-agenda-center")):
        i = text.lower().find(sep)
        if i > 0:
            return text[:i]
    return zip_path.stem


def _workbook_in(z: zipfile.ZipFile) -> str:
    names = [n for n in z.namelist() if n.lower().endswith(".xlsx") and not n.startswith("__MACOSX")]
    preferred = [n for n in names if "master_content_import" in n.lower()]
    if not (preferred or names):
        raise ValueError("The zip has no .xlsx workbook in it; is it the migration tool's import zip?")
    if len(preferred or names) > 1:
        raise ValueError("The zip has more than one workbook: " + ", ".join(preferred or names))
    return (preferred or names)[0]


def find_meetings_json(zip_path: Path, site: str) -> Optional[Path]:
    """The old site's www-meetings.json: next to the zip, or in a folder
    (up to three down) named after the site. Never another site's."""
    folder = zip_path.parent
    hits = sorted(folder.glob("*meetings.json"))
    if len(hits) == 1:
        return hits[0]
    for pattern in ("*/*meetings.json", "*/*/*meetings.json", "*/*/*/*meetings.json"):
        hits = sorted(p for p in folder.glob(pattern) if p.parent.name.lower() == site.lower())
        if hits:
            return hits[0]
    return None


def _item_files(r: dict) -> list[str]:
    return [_s(r.get(c)) for c in ITEM_FILE_COLS if _s(r.get(c))]


def _agenda_files(r: dict) -> list[str]:
    return [x for x in (_s(r.get("Agenda File Name")), _s(r.get("Minutes File Name"))) if x]


def _is_default(r: dict) -> bool:
    return _s(r.get("Is Default Item")).lower() in ("true", "1", "yes")


def plan_batches(source, meetings_json: Optional[Path] = None, zone: str = "Eastern",
                 limit_mb: int = DEFAULT_LIMIT_MB) -> BatchPlan:
    """source: the import zip's path, a ZipSource, or a FolderSource."""
    import openpyxl

    if not isinstance(source, (ZipSource, FolderSource)):
        source = ZipSource(Path(source))
    limit = int(limit_mb) * 2**20
    if limit >= UPLOAD_LIMIT:
        raise ValueError(f"Keep the batch size under {UPLOAD_LIMIT // 2**20} MB: Content Import takes 1 GB at most.")
    wb_name, sizes = source.workbook_name, source.sizes
    wb = openpyxl.load_workbook(io.BytesIO(source.workbook_bytes()), read_only=True, data_only=True)
    plan = BatchPlan(source.path, wb_name, site_name(source.path, Path(wb_name).name), limit, source=source)
    try:
        agendas = _read_sheet(wb, AGENDA_SHEET)
        items = _read_sheet(wb, ITEMS_SHEET)
        for name in wb.sheetnames:
            if name not in (AGENDA_SHEET, ITEMS_SHEET):
                n = sum(1 for r in wb[name].iter_rows(min_row=2, values_only=True)
                        if any(c not in (None, "") for c in r))
                if n:
                    plan.other_sheets[name] = n
    finally:
        wb.close()
    plan.agenda_total, plan.item_total = len(agendas), len(items)

    undated = [r for r in agendas if not _to_date(r.get("Meeting Date"))]
    undated_items = [r for r in items if not _to_date(r.get("Meeting Date")) and not _is_default(r)]
    if (undated or undated_items) and meetings_json:
        opts = ImportOptions(d10_meetings=Path(meetings_json), d10_zone=zone)
        _apply_d10(agendas, items, opts, plan.warnings)
        plan.dates_filled = sum(1 for r in agendas if r.get("_date_from"))
    elif undated or undated_items:
        plan.warnings.append(f"{len(undated)} agenda row(s) and {len(undated_items)} item row(s) have no meeting "
                             "date. Pick the old site's www-meetings.json to fill them in; without it they're left out.")

    by_row = {r["_row"]: r for r in agendas}
    units: dict[tuple, Unit] = {}

    def unit_for(category: str, d: Optional[date]) -> Unit:
        key = (category.strip().lower(), d)
        if key not in units:
            units[key] = Unit(category.strip(), d)
        return units[key]

    def check(sheet: str, r: dict, files: list[str], desc: str) -> Optional[str]:
        cat = _s(r.get("Category"))
        if not cat:
            return "no category"
        missing = [f for f in files if f.lower() not in sizes]
        if missing:
            return "file(s) not found: " + ", ".join(missing)
        return None

    for r in agendas:
        desc = _s(r.get("Agenda Description"))
        d = _to_date(r.get("Meeting Date"))
        reason = check(AGENDA_SHEET, r, _agenda_files(r), desc) or (None if d else "no meeting date")
        if reason:
            plan.left_out.append(LeftOut(AGENDA_SHEET, r["_row"], _s(r.get("Category")), desc, reason))
            continue
        u = unit_for(_s(r.get("Category")), d)
        u.agenda_rows.append(r["_row"])
        u.files.update(f.lower() for f in _agenda_files(r))
        plan.dates[(AGENDA_SHEET, r["_row"])] = d

    for r in items:
        desc = _s(r.get("Name"))
        d = _to_date(r.get("Meeting Date"))
        if not d and r.get("_agenda_row") in by_row:
            d = _to_date(by_row[r["_agenda_row"]].get("Meeting Date"))
        default = _is_default(r)
        reason = check(ITEMS_SHEET, r, _item_files(r), desc) or (None if d or default else "no meeting date")
        if reason:
            plan.left_out.append(LeftOut(ITEMS_SHEET, r["_row"], _s(r.get("Category")), desc, reason))
            continue
        u = unit_for(_s(r.get("Category")), None if default else d)
        u.item_rows.append(r["_row"])
        u.files.update(f.lower() for f in _item_files(r))
        if d and not default:
            plan.dates[(ITEMS_SHEET, r["_row"])] = d

    referenced = set().union(*(u.files for u in units.values())) if units else set()
    plan.unreferenced = sorted(n for n in sizes if n not in referenced)

    # Default items first (they belong to the category, not a meeting), then meetings by date.
    ordered = sorted(units.values(), key=lambda u: (u.meeting_date is not None, u.meeting_date or date.min,
                                                    u.category.lower()))
    cur: Optional[Batch] = None
    for u in ordered:
        own = sum(sizes[f] for f in u.files)
        if own > limit:
            plan.warnings.append(f"{u.category} {u.meeting_date}: its files alone are {own / 2**20:,.0f} MB, more "
                                 "than a batch may hold; it gets a batch of its own and may be refused.")
        new_files = u.files - (cur.files if cur else set())
        if cur is None or (cur.units and cur.size + sum(sizes[f] for f in new_files) > limit):
            cur = Batch(len(plan.batches) + 1)
            plan.batches.append(cur)
            new_files = set(u.files)
        cur.units.append(u)
        cur.files |= new_files
        cur.size += sum(sizes[f] for f in new_files)
    if plan.other_sheets:
        plan.warnings.append("The workbook also has rows for other modules ("
                             + ", ".join(f"{k}: {v}" for k, v in plan.other_sheets.items())
                             + "). They go in batch 01 only, with the files no agenda row names.")
    return plan


def _drop_rows(ws, keep: set[int]) -> None:
    """Delete every data row not in keep, bottom up, a run at a time."""
    drop = [r for r in range(2, ws.max_row + 1) if r not in keep]
    while drop:
        end = drop.pop()
        start = end
        while drop and drop[-1] == start - 1:
            start = drop.pop()
        ws.delete_rows(start, end - start + 1)


def _date_col(ws) -> Optional[int]:
    for c in range(1, ws.max_column + 1):
        if _s(ws.cell(1, c).value).lower() == "meeting date":
            return c
    return None


def _workbook_for(plan: BatchPlan, wb_bytes: bytes, batch: Optional[Batch]):
    """The workbook with dates filled in; for a batch, only that batch's rows."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(wb_bytes))
    for sheet in (AGENDA_SHEET, ITEMS_SHEET):
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        col = _date_col(ws)
        if col:
            for (sh, row), d in plan.dates.items():
                if sh == sheet:
                    cell = ws.cell(row, col)
                    if not _to_date(cell.value):
                        cell.value = datetime(d.year, d.month, d.day)
                        cell.number_format = "m/d/yyyy"
        if batch is not None:
            keep = {r for u in batch.units for r in (u.agenda_rows if sheet == AGENDA_SHEET else u.item_rows)}
            _drop_rows(ws, keep)
    if batch is not None and batch.number > 1:
        for name in plan.other_sheets:
            _drop_rows(wb[name], set())
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def write_batches(plan: BatchPlan, out_dir: Path, log: Callable[[str], None] = lambda m: None,
                  cancelled: Callable[[], bool] = lambda: False) -> list[Path]:
    """Write <site>-agenda-center-import-batchNN.zip for each batch, the full
    dated workbook (for Compare and the to-do list), left-out.csv and
    batches.txt. Returns the zip paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{plan.site}-agenda-center-import-batch*.zip"):
        old.unlink()
    written = []
    src = plan.source
    wb_bytes = src.workbook_bytes()
    wb_base = Path(plan.workbook_name).name
    (out_dir / wb_base).write_bytes(_workbook_for(plan, wb_bytes, None))
    for b in plan.batches:
        if cancelled():
            raise InterruptedError("Stopped.")
        path = out_dir / f"{plan.site}-agenda-center-import-batch{b.number:02d}.zip"
        files = set(b.files)
        if b.number == 1 and plan.other_sheets:
            files |= set(plan.unreferenced)
        log(f"Writing {path.name} ({len(files)} files)...")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zo:
            src.copy_into(zo, files)
            zo.writestr(wb_base, _workbook_for(plan, wb_bytes, b))
        size = path.stat().st_size
        if size >= UPLOAD_LIMIT:
            raise ValueError(f"{path.name} came out at {size / 2**20:,.0f} MB, over Content Import's 1 GB. "
                             "Make the batches again with a smaller size.")
        b.path = path
        written.append(path)
    with open(out_dir / "left-out.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Sheet", "Row", "Category", "Description / Name", "Why it was left out"])
        for x in plan.left_out:
            w.writerow([x.sheet, x.row, x.category, x.description, x.reason])
    (out_dir / "batches.txt").write_text(plan.summary() + "\n", encoding="utf-8")
    return written


def batch_zips(folder: Path) -> list[Path]:
    """The batch zips in a folder, in order."""
    return sorted(Path(folder).glob("*-agenda-center-import-batch*.zip"))


def workbook_categories(folder: Path) -> list[str]:
    """Category names the batches' rows use (from the full dated workbook)."""
    import openpyxl
    names: dict[str, str] = {}
    for wb_path in Path(folder).glob("*Master_Content_Import*.xlsx"):
        wb = openpyxl.load_workbook(wb_path, read_only=True)
        try:
            for sheet in (AGENDA_SHEET, ITEMS_SHEET):
                for r in _read_sheet(wb, sheet):
                    c = re.sub(r"\s+", " ", _s(r.get("Category")))
                    if c:
                        names.setdefault(c.lower(), c)
        finally:
            wb.close()
    return list(names.values())
