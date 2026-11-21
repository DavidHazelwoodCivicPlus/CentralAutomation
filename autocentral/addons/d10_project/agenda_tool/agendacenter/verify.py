"""Check a finished D10 migration against what it should be, meeting by
meeting.

Expected comes from the migration tool's own outputs:
  - the import workbook (AgendaCenter rows, and AgendaCenterItems for the
    items of item-based meetings), less the rows the batches left out
    (left-out.csv);
  - the post-import tasks JSON: the meetings created by hand, the minutes
    to attach, the video links and the item-based agendas that must be
    saved to build their HTML page.

For every expected meeting the site must have, in that category on that
date with that description, an agenda that is:
  - published;
  - with its minutes when it has any, the same file name, Published;
  - with the same Link to Media / Additional Video Link;
  - item-based with the same number of items, and its public HTML agenda
    page opening (not the site's 404 page);
and the site must have no agendas nobody expected.

Reads only, several pages at a time (the logged-in session's own fetch).
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .importdata import _read_sheet, _s, _to_date
from .tasks import CREATE, MEDIA, MINUTES, SAVE, load_tasks

AGENDA_CENTER = "/Admin/AgendaCenter"


@dataclass
class Expected:
    category: str
    meeting_date: date
    description: str
    minutes_file: str = ""
    items: int = 0                 # item-based: how many items
    video: str = ""
    additional_video: str = ""
    html: bool = False             # its public HTML agenda page must open
    source: str = ""


@dataclass
class Problem:
    category: str
    meeting_date: str
    description: str
    agenda_id: str
    check: str
    detail: str


@dataclass
class Verification:
    expected: int = 0
    found: int = 0
    checks: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0]))   # check -> [passed, total]
    problems: list[Problem] = field(default_factory=list)
    extra: list = field(default_factory=list)          # site agendas nobody expected

    @property
    def perfect(self) -> bool:
        return not self.problems and not self.extra and self.found == self.expected

    def summary(self) -> str:
        lines = [f"{self.found} of {self.expected} expected agendas found on the site"
                 + (f"; {len(self.extra)} extra agenda(s) on the site" if self.extra else "; nothing extra")]
        for check, (ok, total) in self.checks.items():
            lines.append(f"  {check}: {ok} of {total}")
        lines.append("100% accurate." if self.perfect else f"{len(self.problems) + len(self.extra)} problem(s).")
        return "\n".join(lines)

    def write_csv(self, path: Path) -> Path:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Check", "Category", "Meeting Date", "Description", "Agenda ID", "Detail"])
            for p in self.problems:
                w.writerow([p.check, p.category, p.meeting_date, p.description, p.agenda_id, p.detail])
            for a in self.extra:
                w.writerow(["extra on site", a.category_name, a.meeting_date, a.description, a.id, "nobody expected it"])
        return path


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def expected_meetings(workbook: Path, tasks_json: Optional[Path], left_out_csv: Optional[Path]) -> list[Expected]:
    import openpyxl
    skip: set[tuple[str, int]] = set()
    if left_out_csv and Path(left_out_csv).is_file():
        with open(left_out_csv, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                skip.add((r["Sheet"], int(r["Row"])))
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    try:
        rows = [r for r in _read_sheet(wb, "AgendaCenter") if ("AgendaCenter", r["_row"]) not in skip]
        items = [r for r in _read_sheet(wb, "AgendaCenterItems") if ("AgendaCenterItems", r["_row"]) not in skip]
    finally:
        wb.close()
    n_items: dict[tuple, int] = defaultdict(int)
    for r in items:
        d = _to_date(r.get("Meeting Date"))
        if d and not str(r.get("Is Default Item") or "").lower() in ("true", "1", "yes"):
            n_items[(_s(r.get("Category")).lower(), d)] += 1
    out: list[Expected] = []
    for r in rows:
        d = _to_date(r.get("Meeting Date"))
        cat = _s(r.get("Category"))
        if not d or not cat:
            continue
        mins = _s(r.get("Minutes File Name")) if str(r.get("Minutes Available")).lower() in ("true", "1", "yes") else ""
        out.append(Expected(cat, d, _s(r.get("Agenda Description")), minutes_file=mins,
                            items=0 if _s(r.get("Agenda File Name")) else n_items.get((cat.lower(), d), 0),
                            source=f"workbook row {r['_row']}"))
    if tasks_json and Path(tasks_json).is_file():
        tasks, _ = load_tasks(Path(tasks_json))
        index: dict[tuple, list[Expected]] = defaultdict(list)
        for e in out:
            index[(e.category.lower(), e.meeting_date)].append(e)
        for t in tasks:
            if not t.category or t.meeting_date is None:
                continue
            if CREATE in t.tasks:
                if not t.manual:
                    continue
                mm = t.manual
                mins = [m.get("fileName") for m in (mm.get("minutes") or []) if m.get("fileName")]
                e = Expected(t.category, t.meeting_date, t.description, minutes_file=mins[0] if mins else "",
                             items=len(mm.get("items") or []), video=t.video, additional_video=t.additional_video,
                             html=bool(mm.get("items")), source=f"tasks: created by hand (node {t.node_id})")
                out.append(e)
                continue
            same = index.get((t.category.lower(), t.meeting_date), [])
            e = next((x for x in same if _norm(x.description) == _norm(t.description)), None) or \
                (same[0] if len(same) == 1 else None)
            if e is None:
                continue
            if MINUTES in t.tasks and t.minutes_file:
                e.minutes_file = t.minutes_file
            if MEDIA in t.tasks:
                e.video, e.additional_video = t.video or e.video, t.additional_video or e.additional_video
            if SAVE in t.tasks:
                e.html = True
    return out


def _field(html: str, name: str) -> str:
    m = re.search(r'name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', html) or \
        re.search(r'value="([^"]*)"[^>]*name="' + re.escape(name) + r'"', html)
    return _unescape(m.group(1)).strip() if m else ""


def _unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s)


def verify(site, expected: list[Expected], categories: Optional[list[str]] = None,
           log=lambda m: None) -> Verification:
    """site: AgendaCenterSite (logged in). categories: limit the "extra on
    site" check to these (default: every category the expectations use)."""
    v = Verification(expected=len(expected))
    cats = {c.name.strip().lower(): c for c in site.categories(refresh=True)}
    want_cats = {e.category.strip().lower() for e in expected} | {c.strip().lower() for c in categories or []}
    agendas: dict[str, list] = {}
    for key in want_cats:
        c = cats.get(key)
        agendas[key] = site.agendas(c) if c else []
    log(f"Read {sum(len(a) for a in agendas.values())} agendas in {len(want_cats)} categories.")

    # match: same category and date; same description first, then whatever is left
    used: set[int] = set()
    matched: list[tuple[Expected, object]] = []
    by_day: dict[tuple, list] = defaultdict(list)
    for key, ags in agendas.items():
        for a in ags:
            by_day[(key, a.meeting_date)].append(a)
    pending = []
    for e in expected:
        pool = [a for a in by_day.get((e.category.strip().lower(), e.meeting_date), []) if a.id not in used]
        a = next((x for x in pool if _norm(x.description) == _norm(e.description)), None)
        if a is None:
            pending.append(e)
            continue
        used.add(a.id)
        matched.append((e, a))
    for e in pending:
        pool = [a for a in by_day.get((e.category.strip().lower(), e.meeting_date), []) if a.id not in used]
        if pool:
            used.add(pool[0].id)
            matched.append((e, pool[0]))
        else:
            v.problems.append(Problem(e.category, str(e.meeting_date), e.description, "", "missing",
                                      f"no agenda in that category on that date ({e.source})"))
    v.found = len(matched)
    v.extra = [a for ags in agendas.values() for a in ags if a.id not in used]

    def check(name, ok, e, a, detail):
        v.checks[name][1] += 1
        if ok:
            v.checks[name][0] += 1
        else:
            v.problems.append(Problem(e.category, str(e.meeting_date), e.description, str(a.id), name, detail))

    for e, a in matched:
        check("published", a.published, e, a, f"status {a.status}")
        if e.minutes_file:
            check("has minutes", a.has_minutes, e, a, f"expected {e.minutes_file}")
        elif a.has_minutes:
            check("no unexpected minutes", False, e, a, "the site agenda has minutes the source doesn't")

    # pages read several at a time: edit pages (links, items), minutes pages, public HTML pages
    edit_need = [(e, a) for e, a in matched if e.video or e.additional_video or e.items]
    mins_need = [(e, a) for e, a in matched if e.minutes_file and a.has_minutes and a.minutes_id]
    html_need = [(e, a) for e, a in matched if e.html and a.published]
    log(f"Reading {len(edit_need)} edit page(s), {len(mins_need)} minutes page(s), {len(html_need)} HTML page(s).")
    pages = site.b.fetch_many([f"{AGENDA_CENTER}/Agenda/Edit/{a.id}?categoryID={a.category_id}" for _, a in edit_need])
    for (e, a), r in zip(edit_need, pages):
        h = r.get("text") or ""
        if e.video or e.additional_video:
            got = (_field(h, "Agenda.VideoLink"), _field(h, "Agenda.AdditionalVideoLink"))
            want = (e.video, e.additional_video)
            check("video links", got == want, e, a, f"site {got}, expected {want}")
        if e.items:
            n = len(re.findall(r'name="agendaItems\.index"', h))
            check("items", n == e.items, e, a, f"site has {n} item(s), expected {e.items}")
    pages = site.b.fetch_many([f"{AGENDA_CENTER}/Minutes/Edit/{a.minutes_id}?agendaID={a.id}" for _, a in mins_need])
    for (e, a), r in zip(mins_need, pages):
        h = r.get("text") or ""
        f, st = _field(h, "Minutes.MinutesFile"), _field(h, "Minutes.Status")
        if not f:   # minutes uploaded with the agenda live on the agenda's edit page
            f = _field(h, "Agenda.Minutes.MinutesFile")
        check("minutes published", st.lower() == "published", e, a, f"minutes status {st or '?'}")
        if f:
            check("minutes file", _norm(Path(f).stem).startswith(_norm(Path(e.minutes_file).stem)[:40]), e, a,
                  f"site {f}, expected {e.minutes_file}")
    pages = site.b.fetch_many([f"/AgendaCenter/ViewFile/Agenda/_{a.meeting_date:%m%d%Y}-{a.id}?html=true"
                               for _, a in html_need])
    for (e, a), r in zip(html_need, pages):
        t = r.get("text") or ""
        ok = r.get("ok") and "Custom404" not in t
        check("HTML agenda page", ok, e, a, f"HTTP {r.get('status')}" + (" (404 page)" if "Custom404" in t else ""))
    return v
