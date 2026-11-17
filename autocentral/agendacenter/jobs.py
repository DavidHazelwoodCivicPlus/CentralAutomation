"""Agenda Center jobs the window runs: built from ticked rows or from a
comparison, run as a dry run first and then for real.

Nothing here knows about Tkinter, so the same jobs can later run from the
command line or from a saved plan.

Safety (same on every site):
  * A live run only happens after a dry run of the same job and the person
    typing the site's address.
  * Publishing answers Notify Me with Don't Send, always.
  * A category is deleted with everything in it (agendas, items, default
    items, folders, workflows), because the site refuses otherwise. Archived
    items count as content: deleting a category always deletes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import InventoryRow
from ..reporting import Reporter
from .compare import MISSING, MISSING_MINUTES, NO_CATEGORY, CompareRow
from .importdata import ImportOptions
from .models import Agenda, Category, Meeting
from .plan import Runner
from .site import AgendaCenterSite

MISSING_FILES = "missing file(s): "      # importdata's problem text for files not found locally


@dataclass
class Selection:
    """Ticked Agenda Center rows, split into whole categories and agendas.
    An agenda inside a ticked category is part of that category's job."""

    categories: dict[int, str] = field(default_factory=dict)          # id -> name
    agendas: dict[int, dict[int, str]] = field(default_factory=dict)  # category id -> {agenda id: name}

    @classmethod
    def from_rows(cls, rows: list[InventoryRow]) -> "Selection":
        sel = cls()
        for r in rows:
            if r.level == "category" and r.category_id is not None:
                sel.categories[r.category_id] = r.name
        for r in rows:
            if r.level == "item" and r.item_id is not None and r.category_id not in sel.categories:
                sel.agendas.setdefault(r.category_id, {})[r.item_id] = r.name
        return sel

    def agenda_count(self) -> int:
        return sum(len(v) for v in self.agendas.values())

    def describe(self) -> str:
        parts = []
        if self.categories:
            parts.append(f"{len(self.categories)} categor{'y' if len(self.categories) == 1 else 'ies'}")
        if self.agendas:
            parts.append(f"{self.agenda_count()} agenda(s)")
        return " and ".join(parts) or "nothing"


class AgendaJobs:
    def __init__(self, browser, reporter: Reporter, live: bool) -> None:
        browser.r = reporter
        self.site = AgendaCenterSite(browser)
        self.r = reporter
        self.live = live
        self.runner = Runner(self.site, reporter, live)

    # ------------------------------------------------------------- helpers
    def _categories(self, ids) -> list[Category]:
        cats = {c.id: c for c in self.site.categories(refresh=True)}
        out = []
        for cid in ids:
            if cid in cats:
                out.append(cats[cid])
            else:
                self.r.warn(f"Category {cid} isn't on the site any more; skipped.")
        return out

    def _agendas(self, wanted: dict[int, dict[int, str]]) -> list[Agenda]:
        out = []
        for cat in self._categories(wanted):
            self.site.ensure_view_options(cat)
            have = {a.id: a for a in self.site.agendas(cat)}
            for aid, name in wanted[cat.id].items():
                if aid in have:
                    out.append(have[aid])
                else:
                    self.r.warn(f"{cat.name}: agenda {aid} ({name}) isn't on the site any more; skipped.")
        return out

    # ---------------------------------------------------------------- jobs
    def delete(self, sel: Selection) -> None:
        """Delete ticked agendas (unpublishing first), then ticked categories
        with everything in them."""
        label = "delete"
        agendas = self._agendas(sel.agendas)
        if agendas:
            if not self.live:
                for a in agendas:
                    self.runner._agenda_res(label, "delete", a, "dry-run",
                                            "would unpublish, then delete" if a.published else "would delete")
            else:
                published = [a for a in agendas if a.published]
                done = self.runner._bulk(label, "unpublish", published) if published else set()
                self.runner._bulk(label, "delete", [a for a in agendas if not a.published or a.id in done])
        if sel.categories:
            self.runner.category_action(label, self._categories(sel.categories), "delete", purge=True)

    def set_published(self, sel: Selection, publish: bool) -> None:
        action = "publish" if publish else "unpublish"
        agendas = [a for a in self._agendas(sel.agendas) if a.published != publish]
        if agendas:
            if not self.live:
                for a in agendas:
                    self.runner._agenda_res(action, action, a, "dry-run", f"would {action}"
                                            + (" (Notify Me: Don't Send)" if publish else ""))
            else:
                self.runner._bulk(action, action, agendas, undo_kind="unpublish" if publish else "republish")
        if sel.categories:
            self.runner.category_action(action, self._categories(sel.categories), action)

    def upload_missing(self, rows: list[CompareRow], meetings: list[Meeting], opts: ImportOptions,
                       publish: bool, create_categories: bool, download_missing: bool,
                       add_minutes: bool = True) -> None:
        """Upload the comparison's missing agendas, and add minutes to agendas
        that are there without them."""
        params = {"publish": publish, "on_missing_category": "create" if create_categories else "skip",
                  "on_duplicate": "skip", "download_missing": download_missing}
        if download_missing:      # a file that can be fetched from the old site isn't a problem
            for m in meetings:
                m.problems = [p for p in m.problems if not (
                    p.startswith(MISSING_FILES) and
                    all(f in m.file_urls for f in p[len(MISSING_FILES):].split(", ")))]
        resolve = self.runner.resolver(opts, meetings, params)
        new =[r.meeting for r in rows if r.outcome in (MISSING, NO_CATEGORY) and r.meeting is not None]
        if new:
            self.r.info(f"Uploading {len(new)} missing agenda(s).")
            self.runner.upload_meetings("upload", new, resolve, params)
        if add_minutes:
            targets = [(r.site.category_id, r.site.id, r.meeting) for r in rows
                       if r.outcome == MISSING_MINUTES and r.site is not None and r.meeting is not None]
            if targets:
                self.r.info(f"Adding minutes to {len(targets)} agenda(s) already on the site.")
                self.runner.add_minutes_to_existing("minutes", targets, resolve, publish=publish)
