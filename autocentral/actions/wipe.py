"""Wipe the whole site: every module's content, every page and every
document, for emptying a test site before a migration.

Each part is that module's own "Delete everything" (the same requests,
checks and results), in this order:
  Agenda Center, Bid Postings, FAQs, News Flash, Archive Center, Calendar,
  Form Center, Parks and Facilities, Resource Directory, Staff Directory,
  Pages (every page except Home and the layout pages, sub-pages first),
  Document Center and Public Images (every folder, file and image; the two
  top folders stay).
It runs as a dry run first like every other action; the live run needs the
site's address typed. A part that fails is reported and the rest go on.
Nobody is notified.
"""

from __future__ import annotations

from ..modules import get as get_module
from ..reporting import Cancelled, Reporter
from .jobs import ModuleJobs, Selection

ORDER = ["agenda", "bids", "faq", "news", "archive", "calendar", "forms", "facilities", "resdir", "staff",
         "pages", "docs"]


class SiteWipe:
    """Same interface as the module jobs, for the run window."""

    def __init__(self, browser, reporter: Reporter, live: bool, parts: list[str] | None = None) -> None:
        self.b, self.r, self.live = browser, reporter, live
        self.parts = parts or ORDER

    def run(self) -> None:
        failed = []
        for key in self.parts:
            self.b.cancel.check()
            label = get_module(key).label
            self.r.info(f"=== {label}")
            try:
                if key == "agenda":
                    self._agenda()
                elif key == "pages":
                    self._pages()
                else:
                    ModuleJobs(self.b, self.r, self.live, key).delete_everything()
            except Cancelled:
                raise
            except Exception as e:           # report it and go on with the rest
                failed.append(label)
                self.r.error(f"{label}: {type(e).__name__}: {e}")
        self.r.info("Wipe " + ("dry run " if not self.live else "") + "finished"
                    + (f"; these parts had problems: {', '.join(failed)}" if failed else "."))

    def _agenda(self) -> None:
        from ..agendacenter.jobs import AgendaJobs, Selection as AgendaSelection
        jobs = AgendaJobs(self.b, self.r, self.live)
        cats = jobs.site.categories(refresh=True)
        self.r.info(f"Agenda Center: {len(cats)} categories on the site.")
        jobs.delete(AgendaSelection(categories={c.id: c.name for c in cats}))

    def _pages(self) -> None:
        jobs = ModuleJobs(self.b, self.r, self.live, "pages")
        cats = jobs.cats(refresh=True)
        jobs._want = set(cats)                       # read every page (status, depth) once
        cats = jobs.cats(refresh=True)
        keep = {cid for cid, c in cats.items() if jobs._protected(c)}
        self.r.info(f"Pages: {len(cats)} on the site; the home page and {len(keep) - (1 in keep)} layout page(s) stay.")
        jobs.delete(Selection("pages", categories={cid: c["name"] for cid, c in cats.items() if cid not in keep}))
