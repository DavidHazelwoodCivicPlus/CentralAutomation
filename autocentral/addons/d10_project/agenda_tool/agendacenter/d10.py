"""The D10 (Drupal) site export: www-meetings.json.

The Content Import workbook the migration produces can come out without
meeting dates (yorksc: the Meeting Date column is blank on every row), and
complex agendas lose their minutes (the workbook row has no Minutes File
Name). The D10 export the workbook was made from has both:

  fields.title[0].value                      meeting title
  fields.field_smart_date[0].value           start, Unix seconds (UTC)
  fields.field_smart_date[0].timezone        '' = the D10 site's own zone
  fields.field_agendas[*].file_name / file_url
  fields.field_minutes_files[*]
  fields.field_packets_files[*]
  fields.field_other_meeting_files[*]

The workbook rows are in the same order as the export's meetings, so row N
is meeting N. Each pairing is still checked (same title, or a shared file
name) and pairs that fail are matched by file name instead, so a reordered
or filtered workbook isn't silently mis-dated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

FILE_FIELDS = {
    "field_agendas": "agenda",
    "field_minutes_files": "minutes",
    "field_packets_files": "packet",
    "field_other_meeting_files": "other",
}

# Standard offsets from UTC (hours). The D10 export leaves the zone blank when
# it is the site's own, so the tool asks which zone the old site was in.
ZONES = {"Eastern": -5, "Central": -6, "Mountain": -7, "Arizona": -7, "Pacific": -8,
         "Alaska": -9, "Hawaii": -10}
IANA = {"America/New_York": "Eastern", "America/Detroit": "Eastern", "America/Indiana/Indianapolis": "Eastern",
        "America/Chicago": "Central", "America/Denver": "Mountain", "America/Boise": "Mountain",
        "America/Phoenix": "Arizona", "America/Los_Angeles": "Pacific", "America/Anchorage": "Alaska",
        "Pacific/Honolulu": "Hawaii"}


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def local_time(epoch: int, zone: str = "Eastern") -> datetime:
    """UTC seconds -> local time in a US zone, with US daylight saving rules
    (2007 on: 2nd Sunday of March to 1st Sunday of November; before: 1st
    Sunday of April to last Sunday of October). No tz database needed."""
    zone = IANA.get(zone, zone) or "Eastern"
    std = ZONES.get(zone, -5)
    utc = datetime.fromtimestamp(int(epoch), tz=timezone.utc).replace(tzinfo=None)
    standard = utc + timedelta(hours=std)
    if zone in ("Arizona", "Hawaii"):
        return standard
    y = standard.year
    if y >= 2007:
        start, end = _nth_sunday(y, 3, 2), _nth_sunday(y, 11, 1)
    else:
        start, end = _nth_sunday(y, 4, 1), _last_sunday(y, 10)
    begins = datetime(start.year, start.month, start.day, 2)
    ends = datetime(end.year, end.month, end.day, 1)       # 2:00 daylight = 1:00 standard
    return standard + timedelta(hours=1) if begins <= standard < ends else standard


@dataclass
class D10File:
    kind: str           # agenda | minutes | packet | other
    name: str
    url: str


@dataclass
class D10Meeting:
    index: int          # position in the export (0-based)
    nid: str
    title: str
    when: Optional[datetime]
    published: bool
    files: list[D10File] = field(default_factory=list)

    @property
    def meeting_date(self) -> Optional[date]:
        return self.when.date() if self.when else None

    def names(self, kind: Optional[str] = None) -> list[str]:
        return [f.name for f in self.files if kind is None or f.kind == kind]


def _first(fields: dict, key: str, sub: str = "value"):
    v = fields.get(key) or []
    return v[0].get(sub) if v and isinstance(v[0], dict) else None


def load_meetings_json(path: Path, zone: str = "Eastern") -> list[D10Meeting]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("data") or data.get("items") or next(iter(data.values()), [])
    out = []
    for i, node in enumerate(data):
        f = node.get("fields") or {}
        sd = (f.get("field_smart_date") or [{}])[0]
        when = None
        if sd.get("value"):
            when = local_time(int(sd["value"]), sd.get("timezone") or zone)
        files = [D10File(kind, x.get("file_name") or "", x.get("file_url") or "")
                 for key, kind in FILE_FIELDS.items() for x in (f.get(key) or []) if x.get("file_name")]
        out.append(D10Meeting(i, str(_first(f, "nid") or node.get("entity_id") or ""),
                              str(_first(f, "title") or "").strip(), when,
                              bool(node.get("is_published", True)), files))
    return out


def find_meetings_json(near: Path) -> Optional[Path]:
    """www-meetings.json (or *-meetings.json) next to the workbook or one folder down."""
    near = Path(near)
    folder = near.parent if near.is_file() else near
    for pattern in ("*meetings.json", "*/*meetings.json", "*/*/*meetings.json"):
        hits = sorted(folder.glob(pattern))
        if hits:
            return hits[0]
    return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def pair_rows(rows: list[dict], meetings: list[D10Meeting], files_of) -> dict[int, tuple[D10Meeting, str]]:
    """Workbook AgendaCenter rows -> D10 meetings. Returns {row index:
    (meeting, how)}, how = "same position" | "file name". files_of(row) gives
    the file names the row mentions."""
    out: dict[int, tuple[D10Meeting, str]] = {}
    used: set[int] = set()
    if len(rows) == len(meetings):
        for i, (r, m) in enumerate(zip(rows, meetings)):
            fs = {x.lower() for x in files_of(r)}
            same_title = _norm(r.get("Agenda Description") or "") == _norm(m.title)
            shared = bool(fs & {x.lower() for x in m.names()})
            if same_title or shared:
                out[i] = (m, "same position")
                used.add(m.index)
    by_file: dict[str, list[D10Meeting]] = {}
    for m in meetings:
        for n in set(x.lower() for x in m.names()):
            by_file.setdefault(n, []).append(m)
    for i, r in enumerate(rows):
        if i in out:
            continue
        cands = {m.index: m for f in files_of(r) for m in by_file.get(f.lower(), []) if m.index not in used}
        if len(cands) == 1:
            m = next(iter(cands.values()))
            out[i] = (m, "file name")
            used.add(m.index)
    return out
