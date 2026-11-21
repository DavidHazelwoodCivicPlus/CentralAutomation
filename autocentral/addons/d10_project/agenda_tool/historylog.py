"""History Log (/Admin/HistoryLog.aspx): who created what, and when.

The module lists mostly don't show a creator or a created date. History Log
records every action with the date and time, the person, and the module's own
IDs ("CID 68 AID 1931", "AMID 38 ADID 4400"). The tool searches it once per
module (all actions, 1/1/2000 to today), reads every result page, and
matches the rows to the inventory by ID.

A big module's log is hundreds of pages, so what was read is kept in
cache/<site>/history_<module>.json. The next scan only reads from the last
scan's date onward and merges.

The search runs in a hidden frame. Searching only reads; it changes nothing.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Optional

from . import parse
from .browser import Browser, SiteError
from .models import HistoryEntry, InventoryRow

HISTORY_PATH = "/Admin/HistoryLog.aspx"

SEARCH_JS = r"""
const v = %s;
const set = (name, value) => { const e = doc.querySelector('[name="' + name + '"]'); if (e) e.value = value; return !!e; };
if (!set('txtModule', v.module) || !set('txtAction', v.action)) return false;
set('dtiStartDateIn', v.start); set('dtiStartTimeIn', '12:00'); set('dtiStartTimeAMPM', 'AM');
set('dtiEndDateIn', v.end); set('dtiEndTimeIn', '11:59'); set('dtiEndTimeAMPM', 'PM');
if (typeof w.SearchForm !== 'function') return false;
w.SearchForm();
"""

PAGE_JS = "if (typeof w.pageNumber !== 'function') return false; w.pageNumber(%d);"


class HistoryLog:
    def __init__(self, browser: Browser, parallel: int = 6, cache_dir: Optional[Path] = None,
                 refresh: bool = False) -> None:
        self.b = browser
        self.parallel = parallel
        self.cache_dir = cache_dir
        self.refresh = refresh          # ignore the saved history and read it all again
        self._cache: dict[tuple, list[HistoryEntry]] = {}

    def search(self, module_id: int | str, action: str = "", start: str = "01/01/2000",
               end: Optional[str] = None) -> list[HistoryEntry]:
        """Every History Log row for a module (action "" = all actions)."""
        key = (str(module_id), action, start, end or "")
        if key in self._cache:
            return self._cache[key]
        end = end or date.today().strftime("%m/%d/%Y")
        search = SEARCH_JS % json.dumps({"module": str(module_id), "action": action, "start": start, "end": end})
        first = self.b.frames([{"src": HISTORY_PATH, "steps": [search]}], parallel=1, page_timeout=120)[0]
        if not first.get("ok"):
            raise SiteError(f"History Log search failed: {first.get('error')}")
        data = parse.history_page(first["pages"][-1])
        if not data["found_table"]:
            raise SiteError("History Log search didn't return a results table.")
        entries = list(data["entries"])
        done, known_max = {1}, data["max_page"]
        failures = 0
        while True:
            pages = [p for p in range(2, known_max + 1) if p not in done]
            if not pages:
                break
            # Each frame searches again, then walks its share of the pages.
            n = min(self.parallel, len(pages))
            chunks = [pages[i::n] for i in range(n)]
            jobs = [{"src": HISTORY_PATH, "capture": "each",
                     "steps": [search] + [PAGE_JS % p for p in chunk]} for chunk in chunks]
            errors = []
            for chunk, res in zip(chunks, self.b.frames(jobs, parallel=n, page_timeout=120)):
                # pages: [0] = blank form, [1] = page 1 again, then the chunk's pages in
                # order; a failed frame keeps the pages it read before failing
                for p, html in zip(chunk, res["pages"][2:]):
                    page = parse.history_page(html)
                    if not page["found_table"]:
                        break
                    entries += page["entries"]
                    known_max = max(known_max, page["max_page"])
                    done.add(p)
                if not res.get("ok"):
                    errors.append(res.get("error") or "unknown error")
            if errors:
                failures += 1
                if failures > 3:
                    raise SiteError(f"History Log paging kept failing: {errors[0]}")
        self._cache[key] = entries
        return entries

    def module_history(self, module_id: int) -> list[HistoryEntry]:
        """All History Log rows for a module, using the saved copy when there
        is one and reading only from its last date onward."""
        path = self.cache_dir / f"history_{module_id}.json" if self.cache_dir else None
        saved: list[HistoryEntry] = []
        start = "01/01/2000"
        if path is not None and path.exists() and not self.refresh:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                saved = [HistoryEntry(**e) for e in data["entries"]]
                start = data["through"]
            except (OSError, ValueError, KeyError, TypeError):
                saved, start = [], "01/01/2000"
        today = date.today().strftime("%m/%d/%Y")
        new = self.search(module_id, "", start, today)
        # the start day is read again; drop the rows we already have
        seen = {(e.when, e.ids, e.action, e.title, e.who) for e in saved}
        merged = saved + [e for e in new if (e.when, e.ids, e.action, e.title, e.who) not in seen]
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"through": today, "entries": [asdict(e) for e in merged]}),
                            encoding="utf-8")
        self.last_new = len(merged) - len(saved)
        self.last_saved = len(saved)
        return merged


# The first History Log row for a record counts as its creation only when it is
# one of these. Anything else (Modify, Publish ...) means the record is older
# than the log, so "created" stays blank instead of being guessed.
CREATE_ACTIONS = ("create", "save", "submit", "add", "copy", "import", "upload")


def join_history(rows: list[InventoryRow], entries: list[HistoryEntry]) -> int:
    """Fill created_on / created_by (and ids) from each record's first History
    Log row, and modified_on / modified_by from its latest when the module's
    list doesn't show them. Returns how many rows were matched.

    The first row is used, not just "Create" rows: on Archive Center, items
    added with Save and Publish are logged as "Save And Publish" only.

    Every entry names the record by its IDs: "AMID 38" is an archive (a
    category), "AMID 38 ADID 4400" an item in it. The shape (the list of
    prefixes) differs per module, so it is learned: for each level, the shape
    that matches the most rows wins. A match needs the record's own id to be
    the last number and, for items, the category id to be the first number,
    so a category "CID 2" can never be taken for form 2."""
    by_shape: dict[tuple, dict[int, list[HistoryEntry]]] = {}
    for e in entries:
        if e.action.lower() == "view" or not e.when:
            continue
        pairs = e.id_pairs
        if not pairs:
            continue
        shape = tuple(k.upper() for k, _ in pairs)
        by_shape.setdefault(shape, {}).setdefault(pairs[-1][1], []).append(e)
    for slot in by_shape.values():
        for lst in slot.values():
            lst.sort(key=lambda e: e.when)

    def fits(r: InventoryRow, shape: tuple, slot: dict) -> Optional[list[HistoryEntry]]:
        lst = slot.get(r.own_id)
        if not lst:
            return None
        e = lst[0]
        if r.level == "category":
            return lst if len(shape) == 1 else None
        if len(shape) < 2:
            # one-pair items only where the module has no categories (Resource Directory)
            return lst if r.category_id is None else None
        # the category must match in at least one row (items can be moved)
        if r.category_id is not None and all(x.id_pairs[0][1] != r.category_id for x in lst):
            return None
        return lst

    matched = 0
    for level in ("category", "item"):
        level_rows = [r for r in rows if r.level == level and r.own_id is not None]
        if not level_rows:
            continue
        votes = Counter({shape: sum(1 for r in level_rows if fits(r, shape, slot))
                         for shape, slot in by_shape.items()})
        if not votes or votes.most_common(1)[0][1] == 0:
            continue
        shape = votes.most_common(1)[0][0]
        for r in level_rows:
            lst = fits(r, shape, by_shape[shape])
            if not lst:
                continue
            first, last = lst[0], lst[-1]
            r.ids = first.ids
            if first.action.lower().startswith(CREATE_ACTIONS):
                r.created_on, r.created_by = first.when, first.who
            else:
                r.extra["history_starts"] = f"{first.when} ({first.action})"
            if not r.modified_on:
                r.modified_on = last.when
            if not r.modified_by:
                r.modified_by = last.who
            matched += 1
    return matched
