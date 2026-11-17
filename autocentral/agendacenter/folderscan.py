"""Turn a folder of agenda and minutes files into an import workbook.

    <root>/
      Board of Commissioners/          <- category (the first folder level)
        BOC_01_08_24_Board_Meeting_Agenda.pdf
        january_8_2024_minutes_-_web.docx
        2023/...                       <- deeper folders are fine; a year in a
                                          folder name fills in names with no year
      Planning and Zoning/
        ...

For every file the scanner works out:
  * the meeting date, from the file name (many formats), else the file's text
  * agenda or minutes, from the file name, else the first page of the file
  * the meeting type (public hearing, called meeting, work session ...), used
    to keep two meetings on the same day apart

Then agendas and minutes for the same category, date and meeting are paired
into one row. Anything uncertain is marked CHECK in the Review column, and the
upload skips CHECK rows until someone changes them to OK.

The result is a normal import workbook (AgendaCenter sheet), so the usual
upload does the rest: dry run, duplicate check, Don't Send, undo. Reads local
files only; never touches the site.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

AGENDA, MINUTES, OTHER, UNKNOWN = "agenda", "minutes", "other", "unknown"
REVIEW_CHECK, REVIEW_OK = "CHECK", "OK"
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
SKIP_EXTS = {".ini", ".db", ".lnk", ".tmp", ".url"}
TEXT_EXTS = {".pdf", ".docx"}

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
             r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_ORD = r"(?:st|nd|rd|th)?"

# Meeting types, most specific first. The label is used for grouping and
# for the agenda description.
MEETING_TYPES = [
    ("public hearing", r"public hearings?"),
    ("work session", r"work ?sessions?|workshops?"),
    ("called meeting", r"(?:special )?called meetings?|special meetings?"),
    ("budget", r"budget (?:meeting|hearing|session)s?"),
    ("emergency meeting", r"emergency meetings?"),
    ("joint meeting", r"joint meetings?"),
]

# Words that make a file an attachment rather than an agenda or minutes.
OTHER_WORDS = r"notice|press release|report|summary|resolution|ordinance|history|template|presentation|" \
              r"budget(?! (?:meeting|hearing|session))|proclamation|exhibit|attachment|map|plat|application"


# ===================================================================== dates
@dataclass
class DateHit:
    value: Optional[date]
    source: str          # name | name (year from folder) | name (year from text) | text | file modified | none
    note: str = ""
    compact6: str = ""   # six digits like 010924 (order decided per folder)


def _year(y: str) -> int:
    n = int(y)
    if len(y) == 4:
        return n
    this = date.today().year % 100
    return 2000 + n if n <= this + 1 else 1900 + n


def _valid(y: int, m: int, d: int) -> Optional[date]:
    try:
        v = date(y, m, d)
    except ValueError:
        return None
    if not (1980 <= v.year <= date.today().year + 2):
        return None
    return v


def normalize(stem: str) -> str:
    s = stem.lower().replace("&amp;", "&").replace("&", " and ")
    s = re.sub(r"[_\-\.,()\[\]#]+", " ", s)
    s = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", s)     # board2024 -> board 2024
    s = re.sub(r"\b(\d{1,2}) (st|nd|rd|th)\b", r"\1\2", s)        # 23 rd -> 23rd
    return re.sub(r"\s+", " ", s).strip()


def name_date(s: str) -> tuple[Optional[date], str, Optional[tuple[int, int]], str]:
    """Best date in a normalized file name.

    Returns (date, note, partial, compact6): partial is (month, day) when the
    name has a month and day but no year; compact6 holds bare six-digit dates
    whose order is decided across the folder."""
    cands: list[tuple[int, int, date, str]] = []   # (score, position, date, note)

    def add(score, pos, v, note=""):
        if v:
            if v > date.today().replace(year=date.today().year + 1):
                score -= 5
            cands.append((score, pos, v, note))

    for m in re.finditer(rf"\b{_MONTH_RE} (\d{{1,2}}){_ORD} (\d{{4}}|\d{{2}})\b", s):
        add(10, m.start(), _valid(_year(m.group(3)), MONTHS[m.group(1)[:3]], int(m.group(2))))
    for m in re.finditer(rf"\b{_MONTH_RE} (\d{{1,2}})((?:19|20)\d{{2}})\b", s):        # april 272026
        add(9, m.start(), _valid(int(m.group(3)), MONTHS[m.group(1)[:3]], int(m.group(2))))
    for m in re.finditer(rf"\b(\d{{1,2}}){_ORD} {_MONTH_RE} (\d{{4}})\b", s):
        add(10, m.start(), _valid(int(m.group(3)), MONTHS[m.group(2)[:3]], int(m.group(1))))
    for m in re.finditer(r"\b(19\d{2}|20\d{2}) (\d{1,2}) (\d{1,2})\b", s):
        add(9, m.start(), _valid(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    # Numbers separated by spaces: every start position, so "hearing 2 08 27 26"
    # can find 08 27 26 as well as 2 08 27.
    for m in re.finditer(r"(?=\b(\d{1,2}) (\d{1,2}) (\d{4}|\d{2})\b)", s):
        a, b, y = m.group(1), m.group(2), m.group(3)
        score = 7 + (1 if len(a) == len(b) == 2 else 0) + (1 if len(y) == 4 else 0)
        v = _valid(_year(y), int(a), int(b))
        if v is None and int(a) > 12:
            v, score = _valid(_year(y), int(b), int(a)), score - 1      # day first
        add(score, m.start(), v)
    for m in re.finditer(r"\b(\d{8,17})\b", s):
        t = m.group(1)
        if len(t) == 8:
            v1 = _valid(int(t[:4]), int(t[4:6]), int(t[6:8]))              # YYYYMMDD
            v2 = _valid(int(t[4:]), int(t[:2]), int(t[2:4]))                # MMDDYYYY
            add(8, m.start(), v1 or v2)
        elif t[:2] in ("19", "20"):
            add(5, m.start(), _valid(int(t[:4]), int(t[4:6]), int(t[6:8])),
                "date taken from a timestamp-like number")
    compact6 = ""
    for m in re.finditer(r"\b(\d{6})\b", s):
        compact6 = m.group(1)
    for m in re.finditer(r"\b(\d{5})\b", s):          # 82426 = 8/24/26
        t = m.group(1)
        add(6, m.start(), _valid(_year(t[3:]), int(t[0]), int(t[1:3])), "five-digit date read as M/DD/YY")
    if cands:
        best = max(cands, key=lambda c: (c[0], -c[1]))
        return best[2], best[3], None, ""
    if compact6:
        return None, "", None, compact6
    m = re.search(rf"\b{_MONTH_RE} (\d{{1,2}}){_ORD}\b", s)
    if m and int(m.group(2)) <= 31:
        return None, "", (MONTHS[m.group(1)[:3]], int(m.group(2))), ""
    return None, "", None, ""


SIX_ORDERS = {"MMDDYY": (0, 2, 4), "DDMMYY": (2, 0, 4), "YYMMDD": (2, 4, 0)}   # (month, day, year) offsets


def six_digit(t: str, order: str) -> Optional[date]:
    mo, da, yr = SIX_ORDERS[order]
    return _valid(_year(t[yr:yr + 2]), int(t[mo:mo + 2]), int(t[da:da + 2]))


def pick_six_order(tokens: list[str]) -> str:
    """The order that reads the most of a folder's six-digit dates; ties go to
    the US order MMDDYY."""
    if not tokens:
        return "MMDDYY"
    counts = {o: sum(1 for t in tokens if six_digit(t, o)) for o in SIX_ORDERS}
    return max(SIX_ORDERS, key=lambda o: (counts[o], o == "MMDDYY"))


def text_dates(text: str) -> list[date]:
    """Dates written out in a document, in order of appearance."""
    s = text.lower()
    out: list[tuple[int, date]] = []
    for m in re.finditer(rf"\b{_MONTH_RE}\s+(\d{{1,2}}){_ORD},?\s+(\d{{4}})\b", s):
        v = _valid(int(m.group(3)), MONTHS[m.group(1)[:3]], int(m.group(2)))
        if v:
            out.append((m.start(), v))
    for m in re.finditer(rf"\b(\d{{1,2}}){_ORD}\s+(?:day\s+of\s+)?{_MONTH_RE},?\s+(\d{{4}})\b", s):
        v = _valid(int(m.group(3)), MONTHS[m.group(2)[:3]], int(m.group(1)))
        if v:
            out.append((m.start(), v))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b", s):
        v = _valid(_year(m.group(3)), int(m.group(1)), int(m.group(2)))
        if v:
            out.append((m.start(), v))
    return [v for _, v in sorted(out, key=lambda x: x[0])]


# ===================================================================== kinds
def name_kind(s: str) -> tuple[str, str]:
    """(kind, note) from a normalized file name."""
    minutes = bool(re.search(r"\b(minutes?|mintues|minuets|mins)\b", s))
    agenda = bool(re.search(r"\b(agendas?|agnd|packet)\b", s))
    if minutes and agenda:
        return UNKNOWN, "name says both agenda and minutes"
    if minutes:
        return MINUTES, ""
    if agenda:
        return AGENDA, ""
    if re.search(rf"\b({OTHER_WORDS})\b", s):
        return OTHER, ""
    return UNKNOWN, ""


_NOT_TITLE = re.compile(r"(approv\w*|adopt\w*|correct\w*|review\w*|amend\w*|accept\w*|read\w*)\s+(of\s+)?(the\s+)?"
                        r"(previous\s+|prior\s+|last\s+|regular\s+|meeting\s+)?(minutes|agenda)", re.I)


_MINUTES_SIGNS = [(r"\bwere (also )?present\b|\bpresent were\b|\b(members|commissioners|staff) present\s*:", 2),
                  (r"\bin attendance\b", 2), (r"\bheld (a|an|its|the)\b", 2),
                  (r"\bcalled (the )?([\w-]+ ){0,4}to order\b", 2), (r"\bmotion (was )?(made|carried|passed)\b", 1),
                  (r"\bseconded\b", 1), (r"\bunanimous", 1), (r"\b(was|were|meeting) adjourned\b", 1),
                  (r"\bvoted\b", 1)]
_AGENDA_SIGNS = [(r"\bcall to order\b", 1), (r"\badjournment\b", 1), (r"\bconsideration of\b", 1),
                 (r"\b(discussion|possible action|action items?)\b", 1), (r"\bnew business\b", 1)]


def text_kind(text: str) -> tuple[str, str]:
    """Agenda or minutes from a document's opening text.

    A title word near the top counts most. Agendas mention "approval of
    minutes" and minutes "adopt the agenda", so those phrases are ignored.
    Minutes are also told apart by how they are written: "were present",
    "held a public hearing", "called the meeting to order"; agendas list
    "Call to Order", "Adjournment", "Consideration of"."""
    head = _NOT_TITLE.sub(" ", text[:3000]).lower()
    score = {MINUTES: 0, AGENDA: 0}
    m = re.search(r"\b(minutes|agenda)\b", head[:400])
    if m:
        score[MINUTES if m.group(1) == "minutes" else AGENDA] += 3
    for pat, w in _MINUTES_SIGNS:
        score[MINUTES] += w * min(2, len(re.findall(pat, head)))
    for pat, w in _AGENDA_SIGNS:
        score[AGENDA] += w * min(2, len(re.findall(pat, head)))
    if score[MINUTES] - score[AGENDA] >= 2:
        return MINUTES, ""
    if score[AGENDA] - score[MINUTES] >= 2:
        return AGENDA, ""
    if score[MINUTES] == score[AGENDA] == 0:
        return UNKNOWN, "the file's text doesn't say agenda or minutes"
    return UNKNOWN, "the file's text could be either an agenda or minutes"


def meeting_type(s: str) -> str:
    for label, pat in MEETING_TYPES:
        m = re.search(rf"\b(?:{pat})\b(?: (\d))?", s)
        if m:
            return f"{label} {m.group(1)}" if label == "public hearing" and m.group(1) else label
    return ""


# ================================================================ file text
def read_text(path: Path, max_chars: int = 6000) -> tuple[str, str]:
    """(text, note). First pages only. Needs pypdf / python-docx; without
    them, or for scans and .doc files, returns "" and says why."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return "", "can't read PDFs (pypdf is not installed)"
            reader = PdfReader(str(path))
            text = ""
            for page in reader.pages[:2]:
                text += (page.extract_text() or "") + "\n"
                if len(text) >= max_chars:
                    break
            if not text.strip():
                return "", "PDF has no text layer (scanned image?)"
            return text[:max_chars], ""
        if ext == ".docx":
            try:
                import docx
            except ImportError:
                return "", "can't read Word files (python-docx is not installed)"
            doc = docx.Document(str(path))
            parts = []
            for sec in doc.sections[:1]:
                parts += [p.text for p in sec.header.paragraphs]
            parts += [p.text for p in doc.paragraphs[:120]]
            for t in doc.tables[:3]:
                for row in t.rows[:10]:
                    parts += [c.text for c in row.cells]
            return "\n".join(parts)[:max_chars], ""
        return "", f"can't read {ext or 'this'} files"
    except Exception as e:
        return "", f"couldn't read the file ({type(e).__name__})"


# =============================================================== the scan
@dataclass
class ScannedFile:
    path: Path
    rel: str
    category: str
    meeting_date: Optional[date] = None
    date_source: str = "none"
    kind: str = UNKNOWN
    kind_source: str = ""
    mtype: str = ""
    notes: list[str] = field(default_factory=list)
    used_in: int = 0             # AgendaCenter row number, 0 = not used

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class ScannedMeeting:
    category: str
    meeting_date: date
    mtype: str
    agenda: Optional[ScannedFile] = None
    minutes: Optional[ScannedFile] = None
    others: list[ScannedFile] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def review(self) -> bool:
        return bool(self.reasons)

    def description(self) -> str:
        """Blank = the upload's usual names. Typed meetings get their type."""
        if not self.mtype:
            return ""
        kind = "Agenda" if self.agenda else "Minutes"
        return f"{self.category} {self.mtype.title()} {kind} (PDF)"

    def confidence(self) -> str:
        files = [f for f in (self.agenda, self.minutes) if f]
        weak = {"none", "name (timestamp)", "name (year from a matching file)"}
        if any(f.date_source in weak or f.kind == UNKNOWN for f in files):
            return "Low"
        if any(f.date_source not in ("name", "name (year from folder)") or f.kind_source != "name" for f in files):
            return "Medium"
        return "High"


@dataclass
class ScanOptions:
    root: Path
    category_from: str = "folder"     # folder = first folder level; root = everything is one category
    read_text: bool = True            # open files whose name lacks the date or the type
    verify_with_text: bool = False    # also open every other file and note disagreements
    category_map: dict = field(default_factory=dict)


@dataclass
class ScanResult:
    root: Path
    files: list[ScannedFile]
    meetings: list[ScannedMeeting]
    warnings: list[str]
    six_orders: dict[str, str]

    def counts(self) -> dict[str, int]:
        return {"files": len(self.files), "rows": len(self.meetings),
                "rows to check": sum(1 for m in self.meetings if m.review),
                "files not used": sum(1 for f in self.files if not f.used_in)}


def _category_for(rel_parts: tuple[str, ...], opts: ScanOptions) -> str:
    if opts.category_from == "root" or len(rel_parts) < 2:
        name = opts.root.name
    else:
        name = rel_parts[0]
    return opts.category_map.get(name, name).strip()


def _folder_year(rel_parts: tuple[str, ...]) -> Optional[int]:
    for part in reversed(rel_parts[:-1]):
        m = re.search(r"\b(19[89]\d|20\d{2})\b", part)
        if m:
            return int(m.group(1))
    return None


def scan(opts: ScanOptions, progress: Optional[Callable[[int, int, str], None]] = None,
         cancel=None) -> ScanResult:
    root = Path(opts.root)
    if not root.is_dir():
        raise ValueError(f"Not a folder: {root}")
    warnings: list[str] = []
    paths = sorted(p for p in root.rglob("*") if p.is_file()
                   and p.name.lower() not in SKIP_NAMES and p.suffix.lower() not in SKIP_EXTS
                   and not p.name.startswith(("~$", ".")))
    files: list[ScannedFile] = []
    six_tokens: dict[str, list[str]] = {}
    partial: dict[int, tuple[int, int]] = {}
    pending6: dict[int, str] = {}

    # ---- pass 1: names
    for p in paths:
        rel_parts = p.relative_to(root).parts
        f = ScannedFile(path=p, rel=str(p.relative_to(root)), category=_category_for(rel_parts, opts))
        s = normalize(p.stem)
        d, note, part, c6 = name_date(s)
        if d:
            f.meeting_date, f.date_source = d, "name"
            if note:
                f.notes.append(note)
            if "timestamp" in note:
                f.date_source = "name (timestamp)"
        elif c6:
            six_tokens.setdefault(f.category, []).append(c6)
            pending6[len(files)] = c6
        elif part:
            partial[len(files)] = part
        f.kind, knote = name_kind(s)
        f.kind_source = "name" if f.kind != UNKNOWN else ""
        if knote:
            f.notes.append(knote)
        f.mtype = meeting_type(s)
        files.append(f)

    # ---- six-digit dates: one order per category folder
    six_orders = {cat: pick_six_order(toks) for cat, toks in six_tokens.items()}
    for idx, tok in pending6.items():
        f = files[idx]
        order = six_orders[f.category]
        v = six_digit(tok, order)
        if v:
            f.meeting_date, f.date_source = v, "name"
            others = [o for o in SIX_ORDERS if o != order and six_digit(tok, o)]
            if others and order != "MMDDYY":
                f.notes.append(f"{tok} read as {order} (the order that fits this folder)")
        else:
            f.notes.append(f"{tok} is not a date in this folder's order ({order})")

    # ---- pass 2: folder years and file text
    for n, f in enumerate(files, 1):
        if cancel:
            cancel.check()
        if progress and n % 10 == 0:
            progress(n, len(files), f"Reading {f.name}")
        rel_parts = f.path.relative_to(root).parts
        if f.meeting_date is None and n - 1 in partial:
            mo, da = partial[n - 1]
            fy = _folder_year(rel_parts)
            if fy:
                v = _valid(fy, mo, da)
                if v:
                    f.meeting_date, f.date_source = v, "name (year from folder)"
        need_text = f.meeting_date is None or f.kind == UNKNOWN or f.date_source == "name (timestamp)"
        if not (opts.read_text and (need_text or opts.verify_with_text)) or f.path.suffix.lower() not in TEXT_EXTS:
            if need_text and opts.read_text and f.path.suffix.lower() not in TEXT_EXTS:
                f.notes.append(f"can't open {f.path.suffix or 'this'} files to look inside")
            continue
        text, tnote = read_text(f.path)
        if tnote:
            f.notes.append(tnote)
            continue
        dates = text_dates(text)
        if f.date_source == "name (timestamp)" and dates:
            f.meeting_date, f.date_source = dates[0], "text"
            f.notes.append("the name is a timestamp; used the date in the text")
        if f.meeting_date is None and dates:
            if n - 1 in partial:      # name had month and day: take the text date that matches them
                mo, da = partial[n - 1]
                match = next((d for d in dates if (d.month, d.day) == (mo, da)), None)
                if match:
                    f.meeting_date, f.date_source = match, "name (year from text)"
            if f.meeting_date is None:
                f.meeting_date, f.date_source = dates[0], "text"
        elif f.meeting_date and dates and opts.verify_with_text and f.meeting_date not in dates[:5]:
            f.notes.append(f"the text mentions {dates[0].isoformat()} near the top, not the name's date")
        if f.kind == UNKNOWN:
            k, knote = text_kind(text)
            if k != UNKNOWN:
                f.kind, f.kind_source = k, "text"
            elif knote:
                f.notes.append(knote)
        elif opts.verify_with_text:
            k, _ = text_kind(text)
            if k not in (UNKNOWN, f.kind) and f.kind in (AGENDA, MINUTES):
                f.notes.append(f"the name says {f.kind} but the text reads like {k}")
        if not f.mtype:
            f.mtype = meeting_type(normalize(text[:400]))

    # ---- name had month and day but no year, and the text didn't help:
    # borrow the year from another file in the folder with that month and day.
    for idx, (mo, da) in partial.items():
        f = files[idx]
        if f.meeting_date is not None:
            continue
        years = sorted({o.meeting_date.year for o in files if o is not f and o.category == f.category
                        and o.meeting_date and (o.meeting_date.month, o.meeting_date.day) == (mo, da)})
        if len(years) == 1:
            f.meeting_date, f.date_source = date(years[0], mo, da), "name (year from a matching file)"
        elif years:
            f.notes.append(f"no year; other files on {mo}/{da} are from {', '.join(map(str, years))}")
    for f in files:
        if f.meeting_date is None and f.kind in (AGENDA, MINUTES):
            f.notes.append("no date in the name or text")

    if progress:
        progress(len(files), len(files), "Pairing agendas and minutes")
    meetings = pair(files, warnings)
    dupes = Counter(f.name.lower() for f in files)
    for m in meetings:
        for f in (m.agenda, m.minutes):
            if f and dupes[f.name.lower()] > 1:
                m.reasons.append(f"another file is also named {f.name}; the upload can't tell them apart")
    return ScanResult(root=root, files=files, meetings=meetings, warnings=warnings, six_orders=six_orders)


def _type_score(a: str, b: str) -> int:
    if a == b:
        return 3
    if not a or not b:
        return 1
    ta, tb = set(a.split()), set(b.split())
    return 2 if ta & tb else 0


def pair(files: list[ScannedFile], warnings: list[str]) -> list[ScannedMeeting]:
    """Group dated files by category and date, then pair agendas with minutes
    by meeting type. Unpaired files become agenda-only or minutes-only rows."""
    days: dict[tuple[str, date], list[ScannedFile]] = {}
    for f in files:
        if f.meeting_date:
            days.setdefault((f.category, f.meeting_date), []).append(f)
    meetings: list[ScannedMeeting] = []
    for (cat, d), group in sorted(days.items(), key=lambda kv: (kv[0][0].lower(), kv[0][1])):
        agendas = [f for f in group if f.kind == AGENDA]
        minutes = [f for f in group if f.kind == MINUTES]
        unknown = [f for f in group if f.kind == UNKNOWN]
        others = [f for f in group if f.kind == OTHER]
        day: list[ScannedMeeting] = []
        # Pair the best-matching agenda/minutes first.
        pairs = sorted(((_type_score(a.mtype, m.mtype), i, j) for i, a in enumerate(agendas)
                        for j, m in enumerate(minutes)), reverse=True)
        used_a, used_m = set(), set()
        single = len(agendas) == 1 and len(minutes) == 1
        for score, i, j in pairs:
            if i in used_a or j in used_m or (score == 0 and not single):
                continue
            used_a.add(i)
            used_m.add(j)
            a, m = agendas[i], minutes[j]
            mt = a.mtype or m.mtype
            day.append(ScannedMeeting(cat, d, mt, agenda=a, minutes=m))
            if score == 0:
                day[-1].reasons.append(f"paired although the names differ ({a.mtype or 'regular'} agenda, "
                                       f"{m.mtype or 'regular'} minutes)")
        for i, a in enumerate(agendas):
            if i not in used_a:
                day.append(ScannedMeeting(cat, d, a.mtype, agenda=a))
        for j, m in enumerate(minutes):
            if j not in used_m:
                day.append(ScannedMeeting(cat, d, m.mtype, minutes=m))
        # Several meetings on one day are only doubtful when a pair was made on a
        # loose type match, or an agenda and a minutes file were both left alone.
        loose = [mt for mt in day if mt.agenda and mt.minutes and _type_score(mt.agenda.mtype, mt.minutes.mtype) < 3]
        lonely = any(mt.agenda and not mt.minutes for mt in day) and any(mt.minutes and not mt.agenda for mt in day)
        if len(day) > 1 and (loose or lonely):
            for mt in day:
                mt.reasons.append(f"{len(day)} meetings on this date; check that the right agenda and minutes "
                                  "are together")
        for f in unknown:
            target = day[0] if len(day) == 1 else None
            note = f"{f.name}: can't tell if it is an agenda or minutes"
            if target is None:
                day.append(ScannedMeeting(cat, d, f.mtype, agenda=f, reasons=[note + " (added as the agenda)"]))
            elif target.agenda is None:
                target.agenda = f
                target.reasons.append(note + " (added as the agenda)")
            elif target.minutes is None:
                target.minutes = f
                target.reasons.append(note + " (added as the minutes)")
            else:
                target.others.append(f)
                target.reasons.append(note)
        if others:
            if len(day) == 1:
                day[0].others += others
                day[0].reasons.append("other files share this date (possible complex agenda or attachments): "
                                      + ", ".join(o.name for o in others))
            elif day:
                for mt in day:
                    mt.reasons.append("other files share this date: " + ", ".join(o.name for o in others))
        for mt in day:
            for f in (mt.agenda, mt.minutes):
                if not f:
                    continue
                why = []
                if f.date_source not in ("name", "name (year from folder)", "name (year from text)"):
                    why.append({"text": "date read from the file's text",
                                "name (timestamp)": "date taken from a timestamp in the name",
                                "name (year from a matching file)": "year borrowed from another file on that day",
                                }.get(f.date_source, f"date from {f.date_source}"))
                if f.kind_source == "text":
                    why.append("agenda/minutes read from the file's text")
                if why:
                    mt.reasons.append(f"{f.name}: " + ", ".join(why))
        meetings += day
    for f in files:
        if not f.meeting_date:
            f.notes.insert(0, "not used: no meeting date found")
    return meetings


# ============================================================ the workbook
REVIEW_COLUMNS = ["Review", "Confidence", "Reasons", "Other Files on This Date", "Agenda File Folder",
                  "Minutes File Folder"]
IMPORT_COLUMNS = ["Category", "Category Description", "Agenda Description", "Meeting Date", "Minutes Available",
                  "Agenda File Name", "Minutes File Name"]


def write_workbook(result: ScanResult, out: Path, text_reads: bool) -> Path:
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AgendaCenter"
    ws.append(IMPORT_COLUMNS + REVIEW_COLUMNS)
    yellow = PatternFill("solid", fgColor="FFF2CC")
    for m in result.meetings:
        row = ws.max_row + 1
        for f in (m.agenda, m.minutes, *m.others):
            if f:
                f.used_in = row
        ws.append([m.category, None, m.description() or None, datetime.combine(m.meeting_date, datetime.min.time()),
                   bool(m.minutes), m.agenda.name if m.agenda else None, m.minutes.name if m.minutes else None,
                   REVIEW_CHECK if m.review else REVIEW_OK, m.confidence(), "; ".join(dict.fromkeys(m.reasons)),
                   ", ".join(o.name for o in m.others) or None,
                   str(m.agenda.path.parent.relative_to(result.root)) if m.agenda else None,
                   str(m.minutes.path.parent.relative_to(result.root)) if m.minutes else None])
        ws.cell(row, 4).number_format = "yyyy-mm-dd"
        if m.review:
            for c in range(1, len(IMPORT_COLUMNS) + len(REVIEW_COLUMNS) + 1):
                ws.cell(row, c).fill = yellow

    fs = wb.create_sheet("Files")
    fs.append(["Category", "File", "Folder", "Meeting Date", "Date From", "Type", "Type From", "Meeting Kind",
               "Used In Row", "Notes"])
    for f in sorted(result.files, key=lambda x: (x.category.lower(), x.meeting_date or date.min, x.name.lower())):
        fs.append([f.category, f.name, str(f.path.parent.relative_to(result.root)),
                   datetime.combine(f.meeting_date, datetime.min.time()) if f.meeting_date else None,
                   f.date_source, f.kind, f.kind_source, f.mtype or None, f.used_in or None,
                   "; ".join(dict.fromkeys(f.notes)) or None])
        fs.cell(fs.max_row, 4).number_format = "yyyy-mm-dd"
        if not f.used_in:
            for c in range(1, 11):
                fs.cell(fs.max_row, c).fill = yellow

    info = wb.create_sheet("ScanInfo")
    info.append(["Setting", "Value"])
    info.append(["Files folder", str(result.root)])
    info.append(["Scanned at", datetime.now().strftime("%Y-%m-%d %H:%M")])
    info.append(["Opened files to read them", "yes" if text_reads else "no"])
    for k, v in result.counts().items():
        info.append([k.capitalize(), v])
    for cat, order in sorted(result.six_orders.items()):
        info.append([f"Six-digit dates in {cat}", order])
    info.append(["How to use", "Fix rows marked CHECK on the AgendaCenter sheet, then change Review to OK. "
                 "The upload skips rows still marked CHECK. Files that were not used are yellow on the Files sheet."])

    widths = {"AgendaCenter": [28, 10, 34, 12, 9, 44, 44, 9, 11, 70, 40, 20, 20],
              "Files": [28, 50, 20, 12, 22, 9, 9, 16, 8, 70], "ScanInfo": [30, 100]}
    for sheet in wb.worksheets:
        sheet.freeze_panes = "A2"
        for c in sheet[1]:
            c.font = Font(bold=True)
        for i, w in enumerate(widths[sheet.title], 1):
            sheet.column_dimensions[get_column_letter(i)].width = w
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out
