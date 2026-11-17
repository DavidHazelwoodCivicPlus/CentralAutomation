"""Load agendas to create from the CivicPlus content-import workbook, enriched
with the D10 migration tool's post-import JSON.

Workbook (Master_Content_Import.xlsx):
  AgendaCenter       Category | Category Description | Agenda Description |
                     Meeting Date | Minutes Available | Agenda File Name | Minutes File Name
  AgendaCenterItems  Name | Category | Meeting Date | Meeting Time | Description |
                     Is Default Item | File Name 1..8      (complex agendas)

JSON (*-post-import-tasks.json): adds media links to matching meetings, and
lists meetings the import could not express ("create-meeting-manually"),
which become extra meetings here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .d10 import load_meetings_json, pair_rows
from .models import COMPLEX, EMPTY, MINUTES_ONLY, SIMPLE, Agenda, ImportItem, Meeting

DEFAULT_NAME_TEMPLATES = {
    SIMPLE: "{category} Regular Meeting Agenda (PDF)",
    MINUTES_ONLY: "{category} Minutes (PDF)",
    COMPLEX: "{category} Regular Meeting Agenda",
    EMPTY: "{category} Regular Meeting Agenda",
}
MAX_DESCRIPTION = 255


@dataclass
class ImportOptions:
    workbook: Optional[Path] = None
    json_path: Optional[Path] = None
    files_dir: Optional[Path] = None          # default: the workbook's folder
    use_source_descriptions: bool = True      # False = always use the name templates
    name_templates: dict = field(default_factory=lambda: dict(DEFAULT_NAME_TEMPLATES))
    include_json_manual_meetings: bool = True
    category_map: dict = field(default_factory=dict)   # {"Name in file": "Name on site"}
    d10_meetings: Optional[Path] = None       # www-meetings.json from the D10 export (dates, lost minutes)
    d10_zone: str = "Eastern"                 # the old site's time zone (dates are stored in UTC)
    d10_minutes: bool = True                  # add minutes the D10 meeting has but the workbook row lacks

    @classmethod
    def from_dict(cls, data: dict) -> "ImportOptions":
        data = dict(data or {})
        for k in ("workbook", "json_path", "files_dir", "d10_meetings"):
            if data.get(k):
                data[k] = Path(data[k])
        templates = dict(DEFAULT_NAME_TEMPLATES)
        templates.update(data.pop("name_templates", {}) or {})
        o = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        o.name_templates = templates
        return o


def _to_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except ValueError:
            pass
    return None


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _key(category: str, d: date, description: str = "") -> tuple:
    return (category.strip().lower(), d, description.strip().lower())


def name_for(meeting_kind: str, category: str, source_description: str, opts: ImportOptions) -> str:
    if opts.use_source_descriptions and source_description.strip():
        return source_description.strip()
    template = opts.name_templates.get(meeting_kind) or DEFAULT_NAME_TEMPLATES[meeting_kind]
    return template.format(category=category)


def _read_sheet(wb, name: str) -> list[dict]:
    if name not in wb.sheetnames:
        return []
    rows = list(wb[name].iter_rows(values_only=True))
    if not rows:
        return []
    header = [(_s(h)) for h in rows[0]]
    out = []
    for idx, r in enumerate(rows[1:], start=2):
        if r is None or all(c is None or _s(c) == "" for c in r):
            continue
        rec = {header[i]: r[i] for i in range(min(len(header), len(r))) if header[i]}
        rec["_row"] = idx
        out.append(rec)
    return out


IMPORT_SHEET = "AgendaCenter"


def workbook_sheets(path: Path) -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def find_import_files(folder: Path) -> tuple[list[Path], list[Path]]:
    """Look in a folder (and one level down) for import workbooks, i.e. ones
    with an AgendaCenter sheet, and D10 post-import JSON files."""
    folder = Path(folder)
    candidates = list(folder.glob("*.xlsx")) + list(folder.glob("*/*.xlsx"))
    workbooks = []
    for p in candidates:
        if p.name.startswith("~$"):
            continue
        try:
            if IMPORT_SHEET in workbook_sheets(p):
                workbooks.append(p)
        except Exception:
            continue
    jsons = [p for p in list(folder.glob("*.json")) + list(folder.glob("*/*.json"))
             if p.name.lower().endswith("post-import-tasks.json")]
    return sorted(workbooks), sorted(jsons)


def check_workbook(path: Path) -> None:
    """Stop with a clear message if this is not an import workbook, instead of
    quietly reading zero agendas from it."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Workbook not found: {path}")
    sheets = workbook_sheets(path)
    if IMPORT_SHEET not in sheets:
        found, _ = find_import_files(path.parent)
        hint = ("\n  Import workbooks found near it:\n    " + "\n    ".join(str(p) for p in found)) if found else ""
        raise ValueError(f"'{path.name}' is not an import workbook: it has no '{IMPORT_SHEET}' sheet "
                         f"(its sheets are: {', '.join(sheets)}).{hint}")


def scan_files_folder(workbook: Optional[Path]) -> Optional[Path]:
    """The files folder recorded by a folder scan (its ScanInfo sheet), or
    None for an ordinary import workbook."""
    if not workbook or not Path(workbook).is_file():
        return None
    import openpyxl
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    try:
        info = {_s(r.get("Setting")): _s(r.get("Value")) for r in _read_sheet(wb, "ScanInfo")}
    finally:
        wb.close()
    return Path(info["Files folder"]) if info.get("Files folder") else None


def files_search(opts: ImportOptions) -> tuple[Optional[Path], bool]:
    """(files folder, look in subfolders). Ordinary workbooks keep the old
    rule: the given files folder, else the workbook's folder, top level only.
    A folder-scan sheet keeps its files in category subfolders."""
    scanned = scan_files_folder(opts.workbook) if opts.workbook else None
    files_dir = opts.files_dir or scanned or (opts.workbook.parent if opts.workbook else None)
    return files_dir, scanned is not None


def load_meetings(opts: ImportOptions) -> tuple[list[Meeting], list[str]]:
    """Return (meetings, warnings). Never touches the site."""
    import openpyxl

    warnings: list[str] = []
    meetings: list[Meeting] = []
    files_dir, deep = files_search(opts)

    def site_category(name: str) -> str:
        return opts.category_map.get(name, name)

    # ---------------------------------------------------------------- workbook
    if opts.workbook:
        check_workbook(opts.workbook)
        wb = openpyxl.load_workbook(opts.workbook, read_only=True, data_only=True)
        agenda_rows = _read_sheet(wb, IMPORT_SHEET)
        if not agenda_rows:
            warnings.append(f"The '{IMPORT_SHEET}' sheet in {Path(opts.workbook).name} has no agenda rows.")
        item_rows = _read_sheet(wb, "AgendaCenterItems")
        wb.close()
        if deep and files_dir and not files_dir.is_dir():
            warnings.append(f"The scan's files folder is missing: {files_dir}")

        if opts.d10_meetings:
            _apply_d10(agenda_rows, item_rows, opts, warnings)

        items_by_meeting: dict[tuple, list[ImportItem]] = {}
        items_by_row: dict[int, list[ImportItem]] = {}
        for r in item_rows:
            if str(r.get("Is Default Item")).strip().lower() == "true":
                continue
            d = _to_date(r.get("Meeting Date"))
            cat = site_category(_s(r.get("Category")))
            if r.get("_agenda_row") and cat:
                files = [_s(r.get(f"File Name {i}")) for i in range(1, 21) if _s(r.get(f"File Name {i}"))]
                t = r.get("Meeting Time")
                items_by_row.setdefault(r["_agenda_row"], []).append(ImportItem(
                    name=_s(r.get("Name")), files=files,
                    meeting_time=t.strftime("%I:%M %p").lstrip("0") if hasattr(t, "strftime") else _s(t),
                    description=_s(r.get("Description"))))
                continue
            if not d or not cat:
                warnings.append(f"AgendaCenterItems row {r['_row']}: missing category or meeting date; ignored.")
                continue
            files = [_s(r.get(f"File Name {i}")) for i in range(1, 21) if _s(r.get(f"File Name {i}"))]
            t = r.get("Meeting Time")
            items_by_meeting.setdefault(_key(cat, d), []).append(ImportItem(
                name=_s(r.get("Name")), files=files,
                meeting_time=t.strftime("%I:%M %p").lstrip("0") if hasattr(t, "strftime") else _s(t),
                description=_s(r.get("Description"))))

        rows_per_day: dict[tuple, int] = {}
        for r in agenda_rows:
            d = _to_date(r.get("Meeting Date"))
            cat = _s(r.get("Category"))
            if d and cat:
                k = _key(site_category(cat), d)
                rows_per_day[k] = rows_per_day.get(k, 0) + 1

        for r in agenda_rows:
            cat_src = _s(r.get("Category"))
            cat = site_category(cat_src)
            d = _to_date(r.get("Meeting Date"))
            if not cat or not d:
                warnings.append(f"AgendaCenter row {r['_row']}: missing category or meeting date; skipped.")
                continue
            m = Meeting(category=cat, meeting_date=d, description="",
                        source_description=_s(r.get("Agenda Description")),
                        agenda_file=_s(r.get("Agenda File Name")),
                        minutes_file=_s(r.get("Minutes File Name")),
                        source="workbook", source_row=r["_row"])
            review = _s(r.get("Review")).upper()
            if review and review not in ("OK", "YES", "Y", "DONE"):
                m.problems.append(f"marked {review} in the Review column; change it to OK to upload "
                                  + (f"({_s(r.get('Reasons'))})" if _s(r.get("Reasons")) else ""))
            if r.get("_date_from"):
                m.notes.append(r["_date_from"])
            d10m = r.get("_d10")
            if d10m is not None:
                m.file_urls = {f.name: f.url for f in d10m.files if f.url}
            if not m.agenda_file and not m.minutes_file and r["_row"] in items_by_row:
                m.items = items_by_row[r["_row"]]
                if any(x for x in r.get("_items_how", []) if x == "order"):
                    m.notes.append("an item with no files was matched to this meeting by order")
            elif not m.agenda_file and not m.minutes_file:
                items = items_by_meeting.get(_key(cat, d), [])
                if items and rows_per_day.get(_key(cat, d), 0) > 1:
                    m.problems.append("several workbook rows share this category+date, so its items can't be "
                                      "matched to one agenda")
                m.items = items
            if d10m is not None and opts.d10_minutes:
                _d10_minutes(m, d10m)
            meetings.append(m)

    # -------------------------------------------------------------------- JSON
    if opts.json_path:
        data = json.loads(Path(opts.json_path).read_text(encoding="utf-8"))
        by_key = {_key(m.category, m.meeting_date, m.source_description): m for m in meetings}
        by_day: dict[tuple, list[Meeting]] = {}
        for m in meetings:
            by_day.setdefault(_key(m.category, m.meeting_date), []).append(m)

        for j in data.get("meetings", []):
            cat = site_category(_s(j.get("category")))
            d = _to_date(j.get("meetingDate"))
            if not cat or not d:
                warnings.append(f"JSON meeting {j.get('sourceNodeId')}: missing category or date; ignored.")
                continue
            media = j.get("mediaLinks") or {}
            tasks = list(j.get("tasks") or [])

            if "create-meeting-manually" in tasks:
                if not opts.include_json_manual_meetings:
                    continue
                mm = j.get("manualMeeting") or {}
                minutes = [_s(x.get("fileName")) for x in (mm.get("minutes") or []) if _s(x.get("fileName"))]
                items = [ImportItem(name=_s(it.get("name")) or "Item",
                                    files=[_s(f.get("fileName")) for f in (it.get("files") or [])
                                           if _s(f.get("fileName"))],
                                    description=_s(it.get("description")))
                         for it in (mm.get("items") or [])]
                agenda_file = _s((mm.get("agendaFile") or {}).get("fileName"))
                m = Meeting(category=cat, meeting_date=d, description="",
                            source_description=_s(j.get("agendaDescription")),
                            video_link=_s(media.get("linkToMedia")),
                            additional_video_link=_s(media.get("additionalVideoLink")),
                            source="json", json_tasks=tasks)
                if items:
                    # Builder agenda: items as given, minutes added after publishing.
                    m.items = items
                    m.attach_minutes_file = minutes[0] if minutes else ""
                    if agenda_file:
                        m.problems.append(f"has both an agenda file ({agenda_file}) and items; "
                                          "an agenda can't be both")
                else:
                    m.agenda_file = agenda_file
                    m.minutes_file = minutes[0] if minutes else ""
                if len(minutes) > 1:
                    m.problems.append(f"{len(minutes)} minutes files; an agenda holds one minutes file")
                local = _s(mm.get("localFolder"))
                if local:
                    m.json_tasks.append(f"files in: {local}")
                meetings.append(m)
                continue

            target = by_key.get(_key(cat, d, _s(j.get("agendaDescription"))))
            if target is None:
                same_day = by_day.get(_key(cat, d), [])
                target = same_day[0] if len(same_day) == 1 else None
            if target is None:
                warnings.append(f"JSON meeting {j.get('sourceNodeId')} ({cat} {d}) matches no workbook row.")
                continue
            target.video_link = target.video_link or _s(media.get("linkToMedia"))
            target.additional_video_link = target.additional_video_link or _s(media.get("additionalVideoLink"))
            target.json_tasks = tasks
            if "attach-minutes" in tasks:
                jm = j.get("minutes") or {}
                fname = _s(jm.get("fileName"))
                if target.kind in (SIMPLE, MINUTES_ONLY) and target.minutes_file:
                    target.problems.append(f"JSON asks to attach minutes ({fname}) but the agenda already "
                                           f"has a minutes file ({target.minutes_file})")
                elif fname:
                    target.attach_minutes_file = fname
                    if _s(jm.get("localFolder")):
                        target.json_tasks.append(f"files in: {_s(jm.get('localFolder'))}")

    # ------------------------------------------------- names and file checks
    search_dirs = [p for p in [files_dir] if p]
    if opts.json_path:
        jdir = Path(opts.json_path).parent
        for m in meetings:
            for t in m.json_tasks:
                if t.startswith("files in: "):
                    search_dirs.append(jdir / t[len("files in: "):])
    for m in meetings:
        m.description = name_for(m.kind, m.category, m.source_description, opts)
        if len(m.description) > MAX_DESCRIPTION:
            m.problems.append(f"description longer than {MAX_DESCRIPTION} characters; it will be cut")
            m.description = m.description[:MAX_DESCRIPTION]
        missing = [f for f in m.all_files() if find_file(f, search_dirs, deep=deep) is None]
        if missing:
            m.problems.append("missing file(s): " + ", ".join(missing))
    return meetings, warnings


def _row_files(r: dict) -> list[str]:
    return [x for x in (_s(r.get("Agenda File Name")), _s(r.get("Minutes File Name"))) if x]


def _apply_d10(agenda_rows: list[dict], item_rows: list[dict], opts: ImportOptions, warnings: list[str]) -> None:
    """Fill missing meeting dates from the D10 export, and tie the complex
    agendas' item rows (which have no dates either) to their agenda row."""
    d10 = load_meetings_json(opts.d10_meetings, opts.d10_zone)
    pairs = pair_rows(agenda_rows, d10, _row_files)
    unpaired = 0
    for i, r in enumerate(agenda_rows):
        if i not in pairs:
            unpaired += 1
            continue
        m, how = pairs[i]
        r["_d10"] = m
        if not _to_date(r.get("Meeting Date")) and m.meeting_date:
            r["Meeting Date"] = m.meeting_date
            r["_date_from"] = f"meeting date from D10 meeting {m.nid} (matched by {how})"
    if len(agenda_rows) != len(d10):
        warnings.append(f"The workbook has {len(agenda_rows)} agenda rows and {Path(opts.d10_meetings).name} "
                        f"has {len(d10)} meetings; rows were matched by file name only.")
    if unpaired:
        warnings.append(f"{unpaired} workbook row(s) couldn't be matched to a D10 meeting.")

    by_file: dict[str, set[int]] = {}
    row_of: dict[int, dict] = {}
    for r in agenda_rows:
        if "_d10" in r:
            row_of[r["_d10"].index] = r
            for n in r["_d10"].names():
                by_file.setdefault(n.lower(), set()).add(r["_d10"].index)
    # Meetings with no files at all on D10 and no files in the workbook: the
    # item rows without files belong to them, in order, per category.
    empty: dict[str, list[dict]] = {}
    for r in agenda_rows:
        if "_d10" in r and not r["_d10"].files and not _row_files(r):
            empty.setdefault(_s(r.get("Category")).lower(), []).append(r)
    unmatched = 0
    for it in item_rows:
        if _to_date(it.get("Meeting Date")) or str(it.get("Is Default Item")).strip().lower() == "true":
            continue
        files = [_s(it.get(f"File Name {i}")) for i in range(1, 21) if _s(it.get(f"File Name {i}"))]
        row = None
        if files:
            hits = set().union(*(by_file.get(f.lower(), set()) for f in files))
            if len(hits) == 1:
                row = row_of[next(iter(hits))]
                row.setdefault("_items_how", []).append("file")
        else:
            queue = empty.get(_s(it.get("Category")).lower()) or []
            if queue:
                row = queue.pop(0)
                row.setdefault("_items_how", []).append("order")
        if row is None:
            unmatched += 1
            continue
        it["_agenda_row"] = row["_row"]
    if unmatched:
        warnings.append(f"{unmatched} AgendaCenterItems row(s) couldn't be tied to a meeting; they're left out.")


def _d10_minutes(m: Meeting, d10m) -> None:
    """The D10 meeting has minutes the workbook row lacks (complex agendas
    always lose theirs). Add them: with the agenda for a simple agenda, or with
    Add Minutes after publishing for a complex one."""
    have = {f.lower() for f in m.all_files()}
    extra = [n for n in d10m.names("minutes") if n.lower() not in have]
    if not extra or m.minutes_file or m.attach_minutes_file:
        return
    if m.agenda_file and not m.items:
        m.minutes_file = extra[0]
    elif m.items:
        m.attach_minutes_file = extra[0]
    else:
        return
    m.notes.append(f"minutes {extra[0]} added from the D10 export (the workbook row had none)")
    if len(extra) > 1:
        m.notes.append(f"D10 has {len(extra)} minutes files; only the first is used")


def find_file(name: str, dirs: list[Path], deep: bool = False) -> Optional[Path]:
    for d in dirs:
        p = d / name
        if p.is_file():
            return p
    low = name.lower()
    for d in dirs:
        if d.is_dir():
            for p in d.iterdir():
                if p.name.lower() == low and p.is_file():
                    return p
    if not deep:
        return None
    # Folder scans keep files in category subfolders: look further down, but
    # only take a name that appears once (two files with one name are ambiguous).
    for d in dirs:
        hits = _tree_index(d).get(low, [])
        if len(hits) == 1:
            return hits[0]
    return None


_TREE_CACHE: dict[str, tuple[float, dict[str, list[Path]]]] = {}


def _tree_index(d: Path) -> dict[str, list[Path]]:
    import time
    key = str(Path(d).resolve()) if Path(d).exists() else str(d)
    hit = _TREE_CACHE.get(key)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    index: dict[str, list[Path]] = {}
    if Path(d).is_dir():
        for p in Path(d).rglob("*"):
            if p.is_file():
                index.setdefault(p.name.lower(), []).append(p)
    _TREE_CACHE[key] = (time.time(), index)
    return index


def _norm_file(name: str) -> str:
    return (name or "").strip().lower()


def is_duplicate(meeting: Meeting, existing: Agenda, details_fn) -> bool:
    """Same category, same meeting date, same files. Several agendas can share
    a category and date on purpose, so the files are what tell them apart."""
    if existing.meeting_date != meeting.meeting_date:
        return False
    if existing.category_name.strip().lower() != meeting.category.strip().lower():
        return False
    det = details_fn(existing)
    if meeting.kind == EMPTY:
        return (not det.agenda_file and not det.minutes_file and det.item_count == 0
                and existing.description.strip().lower() == meeting.description.strip().lower())
    if meeting.kind == COMPLEX:
        want = sorted(_norm_file(f) for it in meeting.items for f in it.files)
        return sorted(_norm_file(f) for f in det.item_files) == want
    agenda_same = _norm_file(det.agenda_file) == _norm_file(meeting.agenda_file) if det.is_simple else not meeting.agenda_file
    return agenda_same and _norm_file(det.minutes_file) == _norm_file(meeting.minutes_file)
