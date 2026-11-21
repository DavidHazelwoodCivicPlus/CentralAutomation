"""Post-import tasks from the D10-to-Central Migration Tool.

The migration tool's Agenda Center workbook leaves out what Central's
importer can't do, and lists it in <site>-agenda-center-post-import-tasks
(.json for scripts, .csv for people). Per meeting, any of:

  create-meeting-manually  the meeting shares a category and date with
                           another one, so the importer can't take it;
                           manualMeeting has its agenda / minutes / items
  attach-minutes           the official minutes of an item-based meeting
                           (minutes.fileName, in <site>-agenda-center-attach-after-import)
  add-media-links          Link to Media / Additional Video Link
  save-to-build-html       an item-based meeting must be opened and saved
                           once, or its public agenda page is a 404

A meeting is found on the site by Category + Meeting Date + Agenda
Description as imported, the same way the migration tool describes it.

Loading the file turns it into to-do list steps (see todo_steps), which the
runner carries out: dry run first, then for real. It works from the JSON
alone, in this order:

  1. attach the minutes (the site only allows it on a published agenda, so
     those drafts are published first)
  2. open each agenda that needs it once: put in its video links and Save,
     which also builds an item-based agenda's HTML page (bulk publish
     doesn't)
  3. publish the agendas it worked on that are still drafts
     (Notify Me: Don't Send)
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .models import ImportItem, Meeting

CREATE = "create-meeting-manually"
MINUTES = "attach-minutes"
MEDIA = "add-media-links"
SAVE = "save-to-build-html"
KINDS = [CREATE, MINUTES, SAVE, MEDIA]          # the order they run in
KIND_LABELS = {CREATE: "create the meetings the importer couldn't",
               MINUTES: "attach minutes",
               SAVE: "open and save item-based agendas (builds their HTML agenda page)",
               MEDIA: "add the video links"}
NO_AGENDA_TEMPLATE = "No-Agenda-Template.pdf"


@dataclass
class PostTask:
    category: str
    meeting_date: Optional[date]
    description: str               # Agenda Description as imported
    node_id: str = ""
    title: str = ""
    source_url: str = ""
    tasks: list[str] = field(default_factory=list)
    minutes_file: str = ""
    minutes_url: str = ""
    local_folder: str = ""
    video: str = ""
    additional_video: str = ""
    overflow: list[str] = field(default_factory=list)
    manual: Optional[dict] = None   # manualMeeting from the JSON

    def label(self) -> str:
        return f"{self.category} {self.meeting_date or '(no date)'} {self.description}"


def _date(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s or "")[:10])
    except ValueError:
        return None


def load_tasks(path: Path) -> tuple[list[PostTask], list[str]]:
    """Read the post-import tasks file (.json or .csv). Returns (tasks, warnings)."""
    path = Path(path)
    warnings: list[str] = []
    out: list[PostTask] = []
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("schemaVersion") not in (None, 1):
            warnings.append(f"Unknown tasks file version {data.get('schemaVersion')}; reading it as version 1.")
        for m in data.get("meetings") or []:
            mins = m.get("minutes") or {}
            media = m.get("mediaLinks") or {}
            out.append(PostTask(
                category=(m.get("category") or "").strip(), meeting_date=_date(m.get("meetingDate")),
                description=(m.get("agendaDescription") or "").strip(), node_id=str(m.get("sourceNodeId") or ""),
                title=m.get("sourceTitle") or "", source_url=m.get("sourceUrl") or "",
                tasks=[t for t in (m.get("tasks") or [])], minutes_file=mins.get("fileName") or "",
                minutes_url=mins.get("sourceUrl") or "", local_folder=mins.get("localFolder") or "",
                video=media.get("linkToMedia") or "", additional_video=media.get("additionalVideoLink") or "",
                overflow=list(media.get("overflow") or []), manual=m.get("manualMeeting")))
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                tasks = [t.strip() for t in (r.get("Tasks") or "").split(";") if t.strip()]
                more = [u.strip() for u in re.split(r"[\s;|]+", r.get("More Video Links (no field for these)") or "")
                        if u.strip()]
                out.append(PostTask(
                    category=(r.get("Category") or "").strip(), meeting_date=_date(r.get("Meeting Date")),
                    description=(r.get("Agenda Description (as imported)") or "").strip(),
                    node_id=r.get("Source Node ID") or "", title=r.get("Source Title") or "", tasks=tasks,
                    minutes_file=r.get("Minutes File Name to attach") or "", minutes_url=r.get("Minutes Source URL") or "",
                    video=r.get("Link to Media") or "", additional_video=r.get("Additional Video Link") or "",
                    overflow=more))
        n = sum(1 for t in out if CREATE in t.tasks)
        if n:
            warnings.append(f"{n} meeting(s) need creating by hand, but the CSV doesn't list their files and "
                            "items. Load the .json version of the tasks file to have those created too.")
    unknown = sorted({k for t in out for k in t.tasks if k not in KINDS})
    if unknown:
        warnings.append("Unknown task type(s), ignored: " + ", ".join(unknown))
    nocat = sum(1 for t in out if not t.category)
    if nocat:
        warnings.append(f"{nocat} meeting(s) have no category in the tasks file; they're skipped.")
    return out, warnings


def counts(tasks: list[PostTask]) -> dict[str, int]:
    return {k: sum(1 for t in tasks if k in t.tasks and (k != CREATE or t.manual)) for k in KINDS}


def categories(tasks: list[PostTask]) -> list[str]:
    return sorted({t.category for t in tasks if t.category}, key=str.lower)


def to_meeting(t: PostTask) -> Optional[Meeting]:
    """create-meeting-manually -> the agenda to create (same shapes the
    upload knows: simple, minutes-only, complex with minutes added later)."""
    mm = t.manual
    if not mm or t.meeting_date is None:
        return None
    urls: dict[str, str] = {}

    def name(f) -> str:
        if not f:
            return ""
        n = (f.get("fileName") or "").strip()
        if n and f.get("sourceUrl"):
            urls[n] = f["sourceUrl"]
        return n

    minutes = [name(x) for x in (mm.get("minutes") or []) if name(x)]
    items = [ImportItem(name=(it.get("name") or "Item").strip(),
                        files=[name(f) for f in (it.get("files") or []) if name(f)],
                        description=(it.get("description") or "").strip())
             for it in (mm.get("items") or [])]
    m = Meeting(category=t.category, meeting_date=t.meeting_date, description=t.description,
                source_description=t.description, source="tasks", video_link=t.video,
                additional_video_link=t.additional_video)
    m.json_tasks = list(t.tasks)
    if t.local_folder or mm.get("localFolder"):
        m.json_tasks.append("files in: " + (mm.get("localFolder") or t.local_folder))
    if items:
        m.items = items
        m.attach_minutes_file = minutes[0] if minutes else ""
        if name(mm.get("agendaFile")):
            m.problems.append("has both an agenda file and items; an agenda can't be both")
    else:
        # Minutes-only meetings don't need the No Agenda template here: the
        # agenda is created bare and the minutes are added to it.
        agenda = name(mm.get("agendaFile"))
        m.agenda_file = "" if agenda == NO_AGENDA_TEMPLATE else agenda
        m.minutes_file = minutes[0] if minutes else ""
    if len(minutes) > 1:
        m.notes.append(f"{len(minutes)} minutes files; an agenda holds one, the first is used")
    m.file_urls = urls
    m.notes.append(f"D10 node {t.node_id}" + (f"; kept in the import instead: node {mm.get('keptInImportInstead')}"
                                              if mm.get("keptInImportInstead") else ""))
    return m


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def find_agenda(t: PostTask, agendas: list) -> tuple[Optional[object], str]:
    """The site agenda this task is about: same meeting date (when the file has
    one) and same description; with one agenda on that date the description
    may differ. Returns (agenda, "") or (None, why)."""
    same_day = [a for a in agendas if t.meeting_date is None or a.meeting_date == t.meeting_date]
    exact = [a for a in same_day if _norm(a.description) == _norm(t.description)]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, f"{len(exact)} agendas on that date have this description"
    if t.meeting_date is not None and len(same_day) == 1:
        return same_day[0], ""
    if not same_day:
        return None, "no agenda on that date in this category"
    return None, f"{len(same_day)} agendas on that date, none with this description"


def todo_steps(tasks_file: Path, files_dir: Optional[Path], tasks: list[PostTask], *, publish: bool = True,
               download_missing: bool = False, create_categories: bool = True, publish_categories: bool = True,
               kinds: Optional[list[str]] = None, tabs: int = 1) -> list[dict]:
    """The to-do list steps (plan step dicts) for one tasks file: the
    categories, then one step that does every chosen task in order (new
    meetings, minutes, open-and-save with video links, publish)."""
    kinds = kinds or KINDS
    c = counts(tasks)
    steps: list[dict] = []
    if create_categories:
        steps.append({"action": "create_categories", "names": categories(tasks), "publish": publish_categories})
    todo = [k for k in KINDS if k in kinds and c[k]]
    if todo:
        steps.append({"action": "post_import_tasks", "kinds": todo, "tasks_file": str(tasks_file),
                      "files_dir": str(files_dir) if files_dir else "", "download_missing": download_missing,
                      "publish": publish, **({"tabs": tabs} if tabs > 1 else {})})
    return steps


NAME_HEADERS = ("name", "category", "category name", "categoryname", "title")


def load_category_names(path: Path) -> tuple[list[str], list[str]]:
    """Category names from a .csv or .xlsx, such as the migration tool's
    <site>-agenda-center-categories.csv: the column headed Name (or
    Category / Category Name / Title), else the first column. Blank and
    repeated names (ignoring case) are dropped. Returns (names, warnings)."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            rows = [["" if v is None else str(v) for v in r] for r in wb.worksheets[0].iter_rows(values_only=True)]
        finally:
            wb.close()
    elif path.suffix.lower() in (".csv", ".txt"):
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    else:
        raise ValueError(f"Pick a .csv or .xlsx file, not {path.suffix or 'a file with no extension'}.")
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [], ["The file is empty."]
    warnings: list[str] = []
    head = [c.strip().lower() for c in rows[0]]
    col = next((head.index(h) for h in NAME_HEADERS if h in head), None)
    if col is None:
        col = 0
        warnings.append("No Name column found; using the first column, header row included.")
        body = rows
    else:
        body = rows[1:]
    names: list[str] = []
    seen: set[str] = set()
    for r in body:
        name = re.sub(r"\s+", " ", r[col] if col < len(r) else "").strip()
        if not name:
            continue
        if name.lower() in seen:
            warnings.append(f"'{name}' is listed more than once; it's created once.")
            continue
        seen.add(name.lower())
        names.append(name)
    return names, warnings

# ------------------------------------------------------- No Agenda template
def no_agenda_template_pdf() -> bytes:
    """The migration tool's one-page "No Agenda" PDF (same text), for any
    task that still points at No-Agenda-Template.pdf."""
    lines = ["No Agenda - There is no agenda available for this meeting.", "Please review the minutes."]
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT\n/F1 16 Tf\n26 TL\n72 700 Td\n" + "".join(
        ("T* " if i else "") + "(" + esc(l) + ") Tj\n" for i, l in enumerate(lines)) + "ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    out, offsets = "%PDF-1.4\n", []
    for i, body in enumerate(objs):
        offsets.append(len(out))
        out += f"{i + 1} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")
