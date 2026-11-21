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
            cats = self._categories(sel.categories)
            self.runner.category_action(action, cats, action)
            if publish:
                # A ticked category means the whole category: its draft agendas too.
                drafts = []
                for cat in cats:
                    self.site.ensure_view_options(cat)
                    found = [a for a in self.site.agendas(cat) if not a.published]
                    self.r.info(f"{cat.name}: {len(found)} draft agenda(s) to publish.")
                    drafts += found
                if drafts:
                    if not self.live:
                        for a in drafts:
                            self.runner._agenda_res(action, action, a, "dry-run",
                                                    "would publish (Notify Me: Don't Send)")
                    else:
                        self.runner._bulk(action, action, drafts, undo_kind="unpublish")

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

    def content_import(self, zips: list, create_categories: bool = True, publish_categories: bool = True,
                       category_names: Optional[list] = None) -> None:
        """Import batch zips through the site's Content Import, one at a time,
        after creating the categories they use. A zip already sent to this
        site (uploaded.json next to it) is never sent again."""
        from pathlib import Path
        from .contentimport import UploadLog, upload_zip

        label = "content import"
        host = self.site.b.host
        if create_categories and category_names:
            self.r.info(f"Checking the {len(category_names)} categories the import uses.")
            self.runner._create_categories(label, {"names": category_names, "publish": publish_categories})
        zips = [Path(z) for z in zips]
        self._import_floor = None           # highest agenda / item IDs before this import (read once, if needed)
        for i, zp in enumerate(zips, 1):
            self.runner.cancel.check()
            log = UploadLog(zp.parent)
            mb = f"{zp.stat().st_size / 2**20:,.0f} MB"
            done = log.find(host, zp)
            if done:
                status = "skipped" if done.get("state") == "done" else "error"
                why = (f"already imported to {host} on {done.get('time')}" if status == "skipped" else
                       f"an earlier upload to {host} was cut off ({done.get('time')}); part of it may be on the "
                       "site. Click 'Import them for me' again: it asks whether that batch finished")
                self.runner._res(label, "import", "zip", description=zp.name, status=status, detail=why)
                continue
            if not self.live:
                self.runner._res(label, "import", "zip", description=zp.name, status="dry-run",
                                 detail=f"would import ({mb}) through Content Import")
                continue
            self.r.info(f"Importing batch {i} of {len(zips)} ({mb}). Central shows nothing until the batch is "
                        "done; a big one takes several minutes.")
            self._import_watched(zp, log, label)
        self.r.info("Content Import brings agendas in as drafts. Publish them with 'Publish ticked…' "
                    "(Status filter: Draft) once the Agenda Center has been read again.")

    def _import_watched(self, zp, log, label: str, depth: int = 0) -> None:
        """Import one zip, watching the site: if no new agenda appears for
        10 minutes the batch is stuck. Then what it had partly made is
        deleted (Content Import would make duplicates otherwise), and the
        batch is split in two and each half imported the same way, down to
        small pieces. A piece that still won't go in is reported."""
        import time
        from pathlib import Path
        from .batchzip import ZipSource, plan_batches, write_batches
        from .contentimport import ImportStalled, UploadLog, upload_zip, zip_units
        host = self.site.b.host
        units, per_cat = zip_units(zp)
        names = {c for c, _ in units}
        cats = [c for c in self.site.categories() if c.name.strip().lower() in names]
        if self._import_floor is None:      # once per import: what was there before it
            ags = [a.id for c in self.site.categories() for a in self.site.agendas(c)]
            its = [i.id for c in self.site.categories() for i in self.site.items(c)]
            self._import_floor = (max(ags, default=0), max(its, default=0))
        floor_a, floor_i = self._import_floor
        # Progress: the agendas in the batch's biggest category (a few pages, once a minute).
        big = max(cats, key=lambda c: per_cat.get(c.name.strip().lower(), 0)) if cats else None
        count = (lambda: len(self.site.agendas(big))) if big else None
        try:
            rep = upload_zip(self.site.b, zp, on_submit=lambda: log.record(host, zp, "started"), progress=count)
        except ImportStalled as e:
            self.r.warn(f"{zp.name}: the import is stuck ({e}). Removing what it made, then trying it in smaller "
                        "pieces.")
            self.site.b.get("/Admin/AgendaCenter")          # lets go of the stuck upload
            last, still = (count() if count else 0), 0
            while count and still < 2:                       # wait until the site has stopped adding
                time.sleep(60)
                now = count()
                still = still + 1 if now == last else 0
                last = now
            gone_a = gone_i = 0
            # What this batch made: newer than anything before the import, on its own meetings.
            for c in cats:
                key = c.name.strip().lower()
                new_a = [a for a in self.site.agendas(c) if a.id > floor_a and (key, a.meeting_date) in units]
                if new_a:
                    pub = [a.id for a in new_a if a.published]
                    if pub:
                        self.site.bulk_agenda_action(c, pub, "unpublish")
                    gone_a += sum(1 for v in self.site.bulk_agenda_action(c, [a.id for a in new_a], "delete").values()
                                  if v == "ok")
                new_i = [i.id for i in self.site.items(c) if i.id > floor_i and (key, i.meeting_date) in units]
                if new_i:
                    gone_i += sum(1 for v in self.site.bulk_item_action(c, new_i, "delete").values() if v == "ok")
            self.r.info(f"{zp.name}: removed the {gone_a} agenda(s) and {gone_i} item(s) it had made.")
            log.record(host, zp, "stalled", f"stuck; removed {gone_a} agenda(s) and {gone_i} item(s); split")
            mb = zp.stat().st_size / 2**20
            if depth >= 3 or mb < 60:
                self.runner._res(label, "import", "zip", description=zp.name, status="error",
                                 detail=f"stuck even at {mb:,.0f} MB; its rows are not on the site")
                return
            plan = plan_batches(ZipSource(zp), limit_mb=max(30, int(mb / 2) + 1))
            parts = write_batches(plan, zp.parent / f"{zp.stem}-split", log=self.r.info)
            self.r.info(f"{zp.name}: split into {len(parts)} piece(s).")
            for part in parts:
                self.runner.cancel.check()
                self.r.info(f"Importing {part.name} ({part.stat().st_size / 2**20:,.0f} MB).")
                self._import_watched(part, UploadLog(part.parent), label, depth + 1)
            log.record(host, zp, "done", f"in {len(parts)} piece(s) after it got stuck")
            return
        except Exception as e:
            self.runner._res(label, "import", "zip", description=zp.name, status="error",
                             detail=f"{type(e).__name__}: {e}")
            raise
        log.record(host, zp, "done", rep.describe())
        if self.r.run_dir is not None:
            (self.r.run_dir / f"{zp.stem}-report.txt").write_text(rep.text, encoding="utf-8")
        self.runner._res(label, "import", "zip", description=zp.name,
                         status="error" if rep.failed else "ok", detail=rep.describe())

    # ------------------------------------------------------- whole migration
    def migrate(self, st, *, create_categories: bool = True, publish_categories: bool = True,
                do_import: bool = True, publish: bool = True, todo: bool = True, empty_first: bool = False,
                verify: bool = True, report: bool = True, tabs: int = 1) -> Optional[Path]:
        """The site part of a D10 migration (d10migrate.MigrationState), each
        step timed:
          0. empty_first: delete EVERY category in the Agenda Center, with
             all its agendas, items, folders and workflows
          1. create the categories (published: Content Import only sees
             published categories)
          2. Content Import of every batch
          3. the to-do list from the tasks JSON: meetings the importer
             couldn't make, saves + video links, publish, minutes
          4. publish every imported draft that's left
          5. verify: every expected meeting against the site (live runs)
          6. report: <site>-migration-report.html in the work folder
        Safe to run again: categories that exist, batches already imported,
        published agendas and finished tasks are skipped. Returns the report."""
        import time
        from pathlib import Path
        from .contentimport import UploadLog
        from .importdata import _read_sheet, _s, _to_date
        from .plan import Plan, Step
        from .tasks import load_tasks, todo_steps

        label = "migrate"
        zips = st.zips()
        steps: list[tuple[str, float, str]] = []

        def timed(name, fn, note=lambda: ""):
            self.r.info(f"=== {name}")
            t0 = time.monotonic()
            fn()
            took = time.monotonic() - t0
            errors = sum(1 for r in self.r.results if r.status == "error")
            steps.append((name, took, (note() or "") + (f" ({errors} error result(s) so far)" if errors else "")))
            self.r.info(f"    {name}: {int(took // 60)}m {int(took % 60):02}s")

        if empty_first:
            def empty():
                cats = self.site.categories(refresh=True)
                self.r.info(f"The Agenda Center has {len(cats)} categories: deleting all of it"
                            + ("" if self.live else " (dry run: nothing is deleted)"))
                self.runner.category_action(label, cats, "delete", purge=True)
            timed("Empty the Agenda Center", empty)
        if create_categories and st.categories:
            timed("Create the categories", lambda: self.runner._create_categories(
                label, {"names": st.categories, "publish": publish_categories}), lambda: f"{len(st.categories)} categories")
        if do_import:
            timed("Content Import", lambda: self.content_import(zips, create_categories=False),
                  lambda: f"{len(zips)} batch(es)")
        host = self.site.b.host
        imported = all((UploadLog(Path(st.batches_dir)).find(host, z) or {}).get("state") == "done" for z in zips)

        if todo and st.tasks_json and Path(st.tasks_json).is_file():
            tasks, warnings = load_tasks(Path(st.tasks_json))
            for w in warnings:
                self.r.warn(w)
            plan_steps = todo_steps(Path(st.tasks_json), Path(st.work), tasks, publish=True, download_missing=True,
                                    create_categories=False, tabs=tabs)
            if not self.live and not imported:
                self.r.info("=== To-do list: runs once the batches are imported")
                for s in plan_steps:
                    step = Step.from_dict(s)
                    self.runner._res(label, "to-do", "step", status="dry-run", detail="after the import: "
                                     + step.describe())
            else:
                timed("To-do list (meetings created by hand, saves + video links, publish, minutes)",
                      lambda: self.runner.run(Plan(name=f"{st.site} post-import tasks",
                                                   steps=[Step.from_dict(s) for s in plan_steps])),
                      lambda: f"{len(tasks)} meeting(s) in the tasks file")
        elif todo:
            self.r.info("No post-import tasks for this site.")

        if publish:
            def publish_rest():
                import openpyxl
                wb = openpyxl.load_workbook(st.workbook, read_only=True, data_only=True)
                try:
                    rows = _read_sheet(wb, "AgendaCenter")
                finally:
                    wb.close()
                wanted: dict[str, set] = {}
                for r in rows:
                    d = _to_date(r.get("Meeting Date"))
                    if d and _s(r.get("Category")):
                        wanted.setdefault(_s(r.get("Category")).lower(), set()).add(d)
                drafts = []
                seen = getattr(self.runner, "agenda_cache", {}) or {}
                for cat in self.site.categories():
                    dates = wanted.get(cat.name.strip().lower())
                    if not dates:
                        continue
                    self.site.ensure_view_options(cat)
                    hit = seen.get(cat.name.strip().lower())
                    ags = hit[1] if hit and hit[0] is not None else self.site.agendas(cat)
                    drafts += [a for a in ags if not a.published and a.meeting_date in dates]
                if not drafts:
                    self.runner._res(label, "publish", "agenda", status="skipped" if imported else "dry-run",
                                     detail="no imported drafts to publish" if imported else
                                     "would publish the drafts the import makes (Notify Me: Don't Send)")
                elif not self.live:
                    for a in drafts:
                        self.runner._agenda_res(label, "publish", a, "dry-run", "would publish (Notify Me: Don't Send)")
                else:
                    self.runner._bulk(label, "publish", drafts, undo_kind="unpublish")
            timed("Publish the rest of the imported agendas", publish_rest)

        verification = None
        if verify and self.live:
            from .verify import expected_meetings, verify as check
            def run_check():
                nonlocal verification
                exp = expected_meetings(Path(st.workbook), Path(st.tasks_json) if st.tasks_json else None,
                                        Path(st.batches_dir) / "left-out.csv" if st.batches_dir else None)
                verification = check(self.site, exp, categories=st.categories, log=self.r.info)
                for line in verification.summary().splitlines():
                    (self.r.info if verification.perfect else self.r.warn)(line)
            timed("Accuracy check against the workbook and tasks JSON", run_check,
                  lambda: "100% accurate" if verification and verification.perfect else
                  f"{len(verification.problems) + len(verification.extra)} problem(s)" if verification else "")

        if report:
            from .migreport import write_report
            path = write_report(st, steps, list(self.r.results), verification, site_host=host, live=self.live)
            self.r.info(f"Report: {path}")
            return path
        return None
