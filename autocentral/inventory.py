"""The combined table: filtering and spreadsheet export."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .models import COLUMNS, InventoryRow, ModuleScan


@dataclass
class RowFilter:
    """One filter for every module. Empty fields match everything."""

    modules: list[str] = field(default_factory=list)      # module keys or the site's labels
    levels: list[str] = field(default_factory=list)       # category / item
    statuses: list[str] = field(default_factory=list)
    created_by: list[str] = field(default_factory=list)   # "unknown" matches rows with no creator
    created_from: str = ""                                # "YYYY-MM-DD" or "YYYY-MM-DD HH:MM"
    created_to: str = ""
    text: str = ""                                        # in name, category or detail
    regex: bool = False
    include_unknown: bool = False   # rows with no Created date pass the date range

    def match(self, r: InventoryRow) -> bool:
        low = lambda xs: {x.strip().lower() for x in xs if x.strip()}
        if self.modules and not ({r.module.lower(), (r.module_label or "").lower()} & low(self.modules)):
            return False
        if self.levels and r.level not in low(self.levels):
            return False
        if self.statuses and (r.status or "").lower() not in low(self.statuses):
            return False
        if self.created_by:
            who = (r.created_by or "unknown").lower()
            if who not in low(self.created_by):
                return False
        if (self.created_from or self.created_to) and (r.created_on or not self.include_unknown):
            if not r.created_on:
                return False
            if self.created_from and r.created_on < _bound(self.created_from, start=True):
                return False
            if self.created_to and r.created_on > _bound(self.created_to, start=False):
                return False
        if self.text:
            hay = " ".join([r.name, r.category, r.detail, *map(str, r.fields.values())])
            if self.regex:
                try:
                    if not re.search(self.text, hay, re.I):
                        return False
                except re.error:
                    return False
            elif self.text.lower() not in hay.lower():
                return False
        return True


def _bound(value: str, start: bool) -> str:
    """'2024-03-05' -> '2024-03-05 00:00:00' (start) / '2024-03-05 23:59:59' (end)."""
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value + (" 00:00:00" if start else " 23:59:59")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", value):
        return value + (":00" if start else ":59")
    return value


def select(rows: Iterable[InventoryRow], f: Optional[RowFilter]) -> list[InventoryRow]:
    return [r for r in rows if f is None or f.match(r)]


def export_xlsx(path: Path, site: str, scans: list[ModuleScan], rows: Optional[list[InventoryRow]] = None) -> Path:
    """Inventory sheet (the rows given, or everything), Summary sheet (per
    module: counts, time, problems) and ScanInfo."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows = rows if rows is not None else [r for s in scans for r in s.rows]
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    extra_cols: list[str] = []          # the modules' own columns, in the order first seen
    for r in rows:
        for k in r.fields:
            if k not in extra_cols:
                extra_cols.append(k)
    ws.append([label for _, label in COLUMNS] + extra_cols)
    for c in ws[1]:
        c.font = Font(bold=True)
    grey = PatternFill("solid", fgColor="EEEEEE")
    for r in rows:
        d = r.as_dict()
        ws.append([d.get(k) if d.get(k) not in (None,) else "" for k, _ in COLUMNS] +
                  [r.fields.get(k, "") for k in extra_cols])
        if r.level == "category":
            for c in ws[ws.max_row]:
                c.fill = grey
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    widths = {"name": 50, "category": 30, "detail": 40, "ids": 22}
    for i, (k, _) in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(k, 16)

    sm = wb.create_sheet("Summary")
    sm.append(["Module", "On this site", "Categories", "Items", "Created by known", "Seconds", "Problems"])
    for c in sm[1]:
        c.font = Font(bold=True)
    for s in scans:
        cats = sum(1 for r in s.rows if r.level == "category")
        known = sum(1 for r in s.rows if r.created_by)
        sm.append([s.label or s.module, "yes" if s.present else "no", cats, len(s.rows) - cats,
                   f"{known} of {len(s.rows)}", round(s.seconds), " | ".join(s.problems)])
    sm.column_dimensions["A"].width = 30
    sm.column_dimensions["G"].width = 100

    info = wb.create_sheet("ScanInfo")
    info.append(["Site", site])
    info.append(["Scanned", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    info.append(["Rows in this sheet", len(rows)])
    info.append(["Note", "Created / Created By come from each record's first History Log row when that row is a "
                         "create or save. Blank = unknown: no History Log rows (e.g. a module's own Import), or the "
                         "record is older than its log. Items brought in with Content Import show whoever ran it."])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
