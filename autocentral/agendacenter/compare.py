"""What the import should have put on the site, against what is there.

Expected = meetings from the import workbook (plus the D10 export).
On site  = the Agenda Center inventory (categories and agendas as read).

Each expected meeting is matched to a site agenda in the same category with
the same meeting date. Several agendas can share a category and date, so the
description decides between them, then whatever is left in order. Each site
agenda is used for one meeting only.

Outcomes per expected meeting:
  missing            no agenda in that category on that date
  no category        the category isn't on the site at all
  missing minutes    the agenda is there, but the import has minutes and the
                     site agenda has none
  on site            there, with minutes if the import has them
And per site agenda nobody matched: "only on site".

Matching is by category, date and description; file names aren't compared
(the site list doesn't show them).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from ..models import InventoryRow
from .models import Meeting

MISSING = "missing"
NO_CATEGORY = "no category"
MISSING_MINUTES = "missing minutes"
ON_SITE = "on site"
ONLY_ON_SITE = "only on site"


@dataclass
class SiteAgenda:
    id: int
    category_id: int
    category: str
    description: str
    meeting_date: Optional[date]
    status: str
    has_minutes: bool


@dataclass
class CompareRow:
    outcome: str
    category: str
    meeting_date: Optional[date]
    description: str
    meeting: Optional[Meeting] = None
    site: Optional[SiteAgenda] = None
    detail: str = ""

    @property
    def expected_minutes(self) -> str:
        m = self.meeting
        return (m.minutes_file or m.attach_minutes_file) if m else ""


@dataclass
class Comparison:
    rows: list[CompareRow] = field(default_factory=list)
    site_categories: list[str] = field(default_factory=list)

    def of(self, outcome: str) -> list[CompareRow]:
        return [r for r in self.rows if r.outcome == outcome]

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in (MISSING, NO_CATEGORY, MISSING_MINUTES, ON_SITE, ONLY_ON_SITE)}
        for r in self.rows:
            out[r.outcome] += 1
        return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _date(s: str) -> Optional[date]:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def site_agendas(rows: list[InventoryRow]) -> tuple[list[SiteAgenda], dict[int, str]]:
    """Agenda Center inventory rows -> site agendas, and category id -> name."""
    cats = {r.category_id: r.name for r in rows if r.level == "category" and r.category_id is not None}
    out = []
    for r in rows:
        if r.level != "item" or r.item_id is None:
            continue
        meeting = r.fields.get("Meeting date") or r.extra.get("meeting") or ""
        out.append(SiteAgenda(r.item_id, r.category_id, cats.get(r.category_id, r.category), r.name,
                              _date(meeting), r.status, r.fields.get("Minutes") == "yes"))
    return out, cats


def compare(meetings: list[Meeting], site: list[SiteAgenda], site_categories: list[str],
            category_map: Optional[dict] = None) -> Comparison:
    cmap = {_norm(k): v for k, v in (category_map or {}).items()}
    cat_names = {_norm(c): c for c in site_categories}
    by_key: dict[tuple, list[SiteAgenda]] = {}
    for a in site:
        by_key.setdefault((_norm(a.category), a.meeting_date), []).append(a)
    used: set[int] = set()
    out = Comparison(site_categories=list(site_categories))

    # Exact description matches first, so a same-day pair can't be crossed over.
    pairs: dict[int, SiteAgenda] = {}
    for idx, m in enumerate(meetings):
        cat = _norm(cmap.get(_norm(m.category), m.category))
        cands = [a for a in by_key.get((cat, m.meeting_date), []) if a.id not in used]
        exact = [a for a in cands if _norm(a.description) == _norm(m.description)]
        if exact:
            pairs[idx] = exact[0]
            used.add(exact[0].id)
    for idx, m in enumerate(meetings):
        if idx in pairs:
            continue
        cat = _norm(cmap.get(_norm(m.category), m.category))
        cands = [a for a in by_key.get((cat, m.meeting_date), []) if a.id not in used]
        if cands:
            pairs[idx] = cands[0]
            used.add(cands[0].id)

    for idx, m in enumerate(meetings):
        site_cat = cmap.get(_norm(m.category), m.category)
        a = pairs.get(idx)
        if a is None:
            if _norm(site_cat) not in cat_names:
                out.rows.append(CompareRow(NO_CATEGORY, m.category, m.meeting_date, m.description, m,
                                           detail=f"category '{site_cat}' isn't on the site"))
            else:
                out.rows.append(CompareRow(MISSING, cat_names[_norm(site_cat)], m.meeting_date, m.description, m))
            continue
        wants_minutes = bool(m.minutes_file or m.attach_minutes_file)
        detail = []
        if _norm(a.description) != _norm(m.description):
            detail.append(f"site calls it '{a.description}'")
        if a.status and a.status != "published":
            detail.append(f"site status: {a.status}")
        outcome = MISSING_MINUTES if wants_minutes and not a.has_minutes else ON_SITE
        out.rows.append(CompareRow(outcome, a.category, m.meeting_date, m.description, m, a, "; ".join(detail)))

    for a in site:
        if a.id not in used:
            out.rows.append(CompareRow(ONLY_ON_SITE, a.category, a.meeting_date, a.description, site=a,
                                       detail="on the site, not in the import file"))
    return out


def export_comparison(path: Path, cmp: Comparison, site: str, sources: list[str]) -> Path:
    """One sheet per outcome, plus Summary."""
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    sm = wb.active
    sm.title = "Summary"
    sm.append(["Site", site])
    sm.append(["Compared", datetime.now().strftime("%Y-%m-%d %H:%M")])
    for s in sources:
        sm.append(["Source", s])
    sm.append([])
    sm.append(["Outcome", "Count"])
    for k, v in cmp.counts().items():
        sm.append([k, v])
    sm.column_dimensions["A"].width = 22
    sm.column_dimensions["B"].width = 90

    head = ["Category", "Meeting date", "Description", "Kind", "Agenda file", "Minutes file",
            "Item files", "Site agenda ID", "Site status", "Site has minutes", "Workbook row", "Detail", "Notes"]
    for outcome in (MISSING, NO_CATEGORY, MISSING_MINUTES, ON_SITE, ONLY_ON_SITE):
        ws = wb.create_sheet(outcome.title()[:31])
        ws.append(head)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in cmp.of(outcome):
            m, a = r.meeting, r.site
            ws.append([r.category, r.meeting_date.isoformat() if r.meeting_date else "", r.description,
                       m.kind if m else "", m.agenda_file if m else "", r.expected_minutes,
                       " | ".join(f"{i.name}: {', '.join(i.files)}" for i in m.items) if m else "",
                       a.id if a else "", a.status if a else "", ("yes" if a.has_minutes else "no") if a else "",
                       m.source_row if m else "", r.detail, "; ".join(m.notes + m.problems) if m else ""])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col, w in zip("ABCDEFGHIJKLM", (28, 12, 45, 10, 40, 40, 50, 12, 11, 10, 10, 40, 60)):
            ws.column_dimensions[col].width = w
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
