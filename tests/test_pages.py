"""The Pages tab: page rows built from a page read, and the page actions
(publish / unpublish / delete) with the in-page requests faked."""

import tkinter as tk
import unittest

from autocentral.actions.jobs import ModuleJobs, Selection
from autocentral.models import InventoryRow, ModuleScan
from autocentral.reporting import CancelToken, Reporter
from autocentral.sitecontent import add_page_rows, page_kind

READ = {"id": 14277, "title": "City Council", "status": "draft", "parent": 14267,
        "path": ["Home", "Leslie Test", "Boards & Commissions"], "version": 41371,
        "widgets": [{"type": "Calendar", "title": "November 2026"}],
        "links": [{"type": "external", "to": "www.cityofleslie.org", "href": "https://www.cityofleslie.org/print/18",
                   "text": "", "widget": ""},
                  {"type": "file", "to": "", "href": "/DocumentCenter/View/12/Agenda", "text": "Agenda", "widget": ""},
                  {"type": "module", "to": "Calendar", "href": "/m/calendar?cat=14", "text": "View All",
                   "widget": "Calendar"}],
        "images": [{"src": "/ImageRepository/Document?documentID=5", "alt": "", "widget": ""}]}


class PageRows(unittest.TestCase):
    def test_page_row_and_its_contents(self):
        ms = ModuleScan("pages", "Pages")
        add_page_rows(ms, READ, "City-Council")
        page, *items = ms.rows
        self.assertEqual((page.level, page.name, page.status, page.category_id), ("category", "City Council", "draft", 14277))
        self.assertEqual(page.extra["parent_id"], 14267)
        self.assertEqual(page.fields["URL"], "/14277/City-Council")
        self.assertEqual(page.fields["Parent"], "Home > Leslie Test > Boards & Commissions")
        self.assertEqual(page.fields["Widgets"], "Calendar (November 2026)")
        self.assertEqual((page.fields["External links"], page.fields["Files"], page.fields["Module links"],
                          page.fields["Images"]), ("1", "1", "1", "1"))
        self.assertIn("links out to www.cityofleslie.org", page.detail)
        self.assertEqual([i.status for i in items], ["widget", "external", "file", "module", "image"])
        self.assertTrue(all(i.category_id == 14277 and i.item_id is None for i in items))
        self.assertIn("(no alt text)", items[-1].detail)
        self.assertIn("in the Calendar widget", items[3].detail)

    def test_home_and_kinds(self):
        ms = ModuleScan("pages", "Pages")
        add_page_rows(ms, {"id": 1, "title": "CP-Training19", "status": "published", "path": []}, "Home")
        self.assertEqual(ms.rows[0].name, "Home")
        self.assertEqual(page_kind("Interior---Layout-Page", ""), "layout page")
        self.assertEqual(page_kind("CustomLinks-3", ""), "custom links")
        self.assertEqual(page_kind("City-Council", "City Council"), "page")

    def test_only_pages_are_selected(self):
        ms = ModuleScan("pages", "Pages")
        add_page_rows(ms, READ, "City-Council")
        sel = Selection.from_rows("pages", ms.rows)
        self.assertEqual(sel.categories, {14277: "City Council"})
        self.assertEqual(sel.items, {})
        self.assertEqual(sel.unresolved, [])


class FakePages:
    """A pretend site's pages: id -> {name, status, parent, depth}."""

    def __init__(self, pages):
        self.pages = {p["id"]: dict(p) for p in pages}
        self.calls = []

    def __call__(self, mod, op, *args):
        self.calls.append((op,) + tuple(tuple(a) if isinstance(a, list) else a for a in args))
        if op == "cats":
            return [{"id": i, "name": p["slug"], "published": None} for i, p in self.pages.items()]
        if op == "read":
            return [{"id": i, "title": self.pages[i]["name"], "status": self.pages[i]["status"],
                     "parent": self.pages[i].get("parent"), "path": ["x"] * self.pages[i].get("depth", 1)}
                    for i in args[0] if i in self.pages]
        if op == "catAct":
            action, ids = args[0], args[1]
            for i in ids:
                if action == "delete":
                    self.pages.pop(i)
                else:
                    self.pages[i]["status"] = "published" if action == "publish" else "unpublished"
            return {"ok": list(ids), "failed": {}}
        if op == "items":
            return []
        raise AssertionError(op)


class FakeBrowser:
    host = "cp-training19.civicplus.com"

    def __init__(self):
        self.r = None
        self.cancel = CancelToken()


def jobs_for(site, live=True):
    rep = Reporter()
    j = ModuleJobs(FakeBrowser(), rep, live, "pages")
    j.relay.call = site
    return j, rep


SITE = [{"id": 1, "slug": "Home", "name": "Home", "status": "published", "depth": 0},
        {"id": 96, "slug": "Interior---Layout-Page", "name": "Interior Layout Page", "status": "published"},
        {"id": 10, "slug": "Parks", "name": "Parks", "status": "draft", "parent": 1, "depth": 1},
        {"id": 11, "slug": "Trails", "name": "Trails", "status": "draft", "parent": 10, "depth": 2},
        {"id": 12, "slug": "About", "name": "About", "status": "published", "parent": 1, "depth": 1}]


class PageJobs(unittest.TestCase):
    def test_publish_reads_only_the_ticked_pages(self):
        site = FakePages(SITE)
        j, rep = jobs_for(site)
        j.set_published(Selection("pages", categories={10: "Parks", 12: "About"}), True)
        reads = [c for c in site.calls if c[0] == "read"]
        self.assertEqual(sorted(reads[0][1]), [10, 12])
        self.assertEqual(site.pages[10]["status"], "published")
        self.assertEqual(sorted((r.target_id, r.status) for r in rep.results), [("10", "ok"), ("12", "skipped")])
        self.assertEqual(rep.undo[0]["undo"], "unpublish")

    def test_home_and_layout_pages_are_never_unpublished_or_deleted(self):
        site = FakePages(SITE)
        j, rep = jobs_for(site)
        j.set_published(Selection("pages", categories={1: "Home", 96: "Layout"}), False)
        j.delete(Selection("pages", categories={1: "Home", 96: "Layout"}))
        self.assertFalse([c for c in site.calls if c[0] == "catAct"])
        self.assertEqual({r.status for r in rep.results}, {"skipped"})
        self.assertTrue(any("home page" in r.detail for r in rep.results))
        self.assertTrue(any("layout page" in r.detail for r in rep.results))

    def test_delete_goes_deepest_first_without_unpublishing(self):
        site = FakePages(SITE)
        j, rep = jobs_for(site)
        j.delete(Selection("pages", categories={10: "Parks", 11: "Trails"}))
        acts = [c for c in site.calls if c[0] == "catAct"]
        self.assertEqual(acts, [("catAct", "delete", (11, 10))])
        self.assertNotIn(10, site.pages)
        self.assertEqual([r.status for r in rep.results], ["ok", "ok"])


class PagesTab(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(str(e))
        self.root.withdraw()
        from autocentral.gui import App
        self.app = App(self.root)

    def tearDown(self):
        self.root.destroy()

    def test_pages_tab(self):
        ms = ModuleScan("pages", "Pages", read_at="2026-11-20 13:00")
        add_page_rows(ms, READ, "City-Council")
        self.app.set_module(ms, "pages")
        tab = self.app.tabs["pages"]
        shown = list(tab.tree["displaycolumns"])
        self.assertNotIn("created_on", shown)          # no History Log read: no Created column
        labels = [tab.tree.heading(c)["text"] for c in shown]
        self.assertIn("URL", labels)
        self.assertIn("External links", labels)
        texts = {b["text"] for b in self.app.module_actions["pages"].buttons}
        self.assertIn("Delete ticked…", texts)
        self.assertNotIn("Delete everything in this module…", texts)
        self.assertNotIn("Create categories…", texts)
        self.assertIn("st_draft", tab.tree.item("0", "tags"))


if __name__ == "__main__":
    unittest.main()
