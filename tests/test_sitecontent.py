"""Linked pages, file details, Document Center, the cross-reference,
the link check, the site map export and the show/hide tick boxes."""

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import openpyxl

from autocentral import linkcheck
from autocentral.models import ModuleScan
from autocentral.sitecontent import (add_doc_rows, add_page_rows, cross_reference, doc_id_of, export_sitemap,
                                     file_name_of, tree_depths)

HOST = "cp-training19.civicplus.com"


def pages_scan():
    ms = ModuleScan("pages", "Pages")
    add_page_rows(ms, {"id": 1, "title": "CP-Training19", "status": "published", "path": [], "links": [], "images": []},
                  "Home", HOST)
    add_page_rows(ms, {"id": 10, "title": "Parks", "status": "draft", "parent": 1, "path": ["Home"],
                       "links": [{"type": "file", "to": "", "href": "/DocumentCenter/View/500/Park-Map", "text": "Park map"},
                                 {"type": "file", "to": "", "href": "https://old.example.gov/files/rules%20v2.pdf",
                                  "text": "Rules"},
                                 {"type": "phone", "to": "", "href": "tel:555", "text": "555"},
                                 {"type": "external", "to": "old.example.gov", "href": "https://old.example.gov/x", "text": "x"}],
                       "images": [{"src": "/ImageRepository/Document?documentID=900", "alt": ""}]}, "Parks", HOST)
    add_page_rows(ms, {"id": 11, "title": "Pay Online", "status": "linked page", "parent": 10, "path": ["Home", "Parks"],
                       "linkTo": "https://pay.example.com/", "linkToType": "external: pay.example.com"}, "Pay-Online", HOST)
    return ms


def docs_scan():
    ms = ModuleScan("docs", "Document Center")
    add_doc_rows(ms, {"id": 1, "name": "Document Center"}, [{"id": 5, "name": "Parks", "parent": 1, "path": []}],
                 [{"id": 500, "folder": 5, "name": "Park Map", "file": "park-map-2024.pdf", "type": ".pdf",
                   "status": "Published", "link": "/DocumentCenter/View/500/Park-Map", "refs": 1},
                  {"id": 501, "folder": 5, "name": "Unused", "file": "old.pdf", "type": ".pdf", "status": "Published",
                   "link": "/DocumentCenter/View/501/Unused", "refs": 0}])
    add_doc_rows(ms, {"id": 13, "name": "Public Images"}, [],
                 [{"id": 900, "folder": 13, "name": "Pool", "file": "pool.jpg", "type": ".jpg", "status": "Published",
                   "link": "/ImageRepository/Document?documentID=900", "refs": 0}])
    return ms


class Shapes(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(doc_id_of("/DocumentCenter/View/500/Park-Map"), 500)
        self.assertEqual(doc_id_of("https://x.gov/ImageRepository/Document?documentID=900"), 900)
        self.assertIsNone(doc_id_of("https://old.example.gov/files/a.pdf"))
        self.assertEqual(file_name_of("https://old.example.gov/files/rules%20v2.pdf"), "rules v2.pdf")
        self.assertEqual(file_name_of("/DocumentCenter/View/500/Park-Map"), "Park-Map")

    def test_page_rows(self):
        ms = pages_scan()
        rows = {(r.level, r.name): r for r in ms.rows}
        self.assertNotIn("phone", {r.status for r in ms.rows})          # phone numbers aren't listed
        rules = rows[("item", "Rules")]
        self.assertEqual((rules.fields["File name"], rules.fields["Shown as"], rules.fields["Internal / external"]),
                         ("rules v2.pdf", "Rules", "external"))
        self.assertEqual(rows[("item", "Park map")].fields["Internal / external"], "internal")
        linked = rows[("category", "Pay Online")]
        self.assertEqual(linked.status, "linked page")
        self.assertEqual(linked.fields["Links to"], "https://pay.example.com/")
        self.assertIn("linked page", linked.fields["Kind"])

    def test_cross_reference(self):
        pages, docs = pages_scan(), docs_scan()
        cross_reference(pages, docs)
        park = next(r for r in pages.rows if r.name == "Park map")
        self.assertEqual(park.fields["File name"], "park-map-2024.pdf")
        self.assertIn("Document Center: Parks (ID 500)", park.fields["In Document Center"])
        d = {r.item_id: r for r in docs.rows if r.level == "item"}
        self.assertEqual((d[500].fields["Used on pages"], d[500].fields["Pages"]), ("1", "Parks"))
        self.assertEqual(d[501].fields["Used on pages"], "0")
        self.assertEqual(d[900].fields["Used on pages"], "1")          # the image on Parks

    def test_site_map_export_puts_pages_in_tiers(self):
        ms = pages_scan()
        parent = {}
        cats = {r.category_id: i for i, r in enumerate(ms.rows) if r.level == "category"}
        for i, r in enumerate(ms.rows):
            parent[i] = cats.get(r.extra.get("parent_id")) if r.level == "category" else cats.get(r.category_id)
        depths = tree_depths(ms.rows, parent)
        with tempfile.TemporaryDirectory() as tmp:
            out = export_sitemap(Path(tmp) / "map.xlsx", "https://" + HOST, ms.rows, depths)
            ws = openpyxl.load_workbook(out)["Site Map"]
            rows = list(ws.iter_rows(values_only=True))
        self.assertEqual(rows[0][:3], ("Tier 1", "Tier 2", "Tier 3"))
        self.assertEqual(rows[1][0], "Home")
        self.assertEqual(rows[2][1], "Parks")
        self.assertEqual(rows[3][2], "Pay Online")
        self.assertIn("https://" + HOST + "/10/Parks", rows[2])


class LinkCheck(unittest.TestCase):
    def test_each_address_checked_once_and_marked(self):
        ms = pages_scan()

        class Relay:
            calls = []

            def call(self, mod, op, urls):
                Relay.calls.append(list(urls))
                return {u: {"ok": "Park" in u, "why": "" if "Park" in u else "page not found"} for u in urls}
        with mock.patch.object(linkcheck, "check_external", side_effect=lambda u: (u.endswith(".pdf"), "HTTP 404")) as ext:
            res = linkcheck.check_rows(ms.rows, HOST, Relay(), lambda m: None, workers=2)
        self.assertEqual(sorted(Relay.calls[0]), ["/DocumentCenter/View/500/Park-Map", "/ImageRepository/Document?documentID=900"])
        self.assertEqual(ext.call_count, 2)                            # the two outside addresses, once each
        rows = {r.name: r for r in ms.rows}
        self.assertEqual(rows["Park map"].fields["Works"], "yes")
        self.assertEqual(rows["x"].fields["Works"], "no: HTTP 404")
        self.assertEqual(rows["Parks"].fields["Broken links"], "2")    # x and the image
        self.assertEqual(len(res), 4)


class Window(unittest.TestCase):
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

    def test_show_boxes_hide_statuses_and_their_rows(self):
        self.app.set_module(pages_scan(), "pages")
        tab = self.app.tabs["pages"]
        self.assertIn("draft", tab.show_vars)
        self.assertIn("linked page", tab.show_vars)
        n = len(tab.shown)
        tab.show_vars["image"].set(False)
        tab.refresh()
        self.assertEqual(len(tab.shown), n - 1)
        tab.show_vars["draft"].set(False)              # the draft page, its links and the page under it
        tab.refresh()
        names = {tab.scan.rows[i].name for i in tab.shown}
        self.assertNotIn("Park map", names)
        self.assertIn("Pay Online", names)              # a published / linked sub-page still shows

    def test_documents_tab_has_actions_and_is_cross_referenced(self):
        self.app.set_module(pages_scan(), "pages")
        self.app.set_module(docs_scan(), "docs")
        bar = self.app.module_actions["docs"]                 # publish / unpublish / delete, no Create categories
        labels = [str(b.cget("text")) for b in bar.buttons]
        self.assertIn("Delete ticked…", labels)
        self.assertNotIn("Create categories…", labels)
        d = [r for r in self.app.tabs["docs"].scan.rows if r.item_id == 500][0]
        self.assertEqual(d.fields["Used on pages"], "1")

    def test_name_column_keeps_its_width(self):
        tab = self.app.tabs["pages"]
        tab._name_width = 150
        self.app.set_module(pages_scan(), "pages")
        self.assertEqual(int(tab.tree.column("#0", "width")), 150)
        self.assertFalse(int(tab.tree.column("#0", "stretch")))


if __name__ == "__main__":
    unittest.main()


class RoundThree(unittest.TestCase):
    """Tab order and names, file type boxes, opening in your own browser, the countdown."""

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

    def test_tab_order_and_fixed_names(self):
        names = [self.app.nb.tab(t, "text") for t in self.app.nb.tabs()]
        self.assertEqual(names[:3], ["Overview", "Pages", "Document Center"])
        ms = ModuleScan("resdir", "Community Resource Directory")
        self.app.set_module(ms, "resdir")
        self.assertEqual(self.app.nb.tab(self.app.tabs["resdir"].frame, "text"), "Resource Directory")

    def test_file_type_boxes(self):
        self.app.set_module(docs_scan(), "docs")
        tab = self.app.tabs["docs"]
        self.assertEqual(set(tab.type_vars), {"pdf", "jpg"})
        n = len(tab.shown)
        tab.type_vars["pdf"].set(False)
        tab.refresh()
        self.assertEqual(len(tab.shown), n - 2)
        self.app.set_module(pages_scan(), "pages")
        self.assertIn("pdf", self.app.tabs["pages"].type_vars)

    def test_double_click_opens_your_browser(self):
        self.app.site.set("https://" + HOST)
        with mock.patch("webbrowser.open") as wb:
            self.app.open_in_chrome("/10/Parks", live_edit=True)
        wb.assert_called_once_with("https://" + HOST + "/10/Parks")

    def test_countdown(self):
        from autocentral.gui import _mmss
        self.assertEqual(_mmss(125), "2:05")
        self.app._set_busy(True, "Reading Pages")
        self.app._progress(0, 100, "pages")
        self.app._progress_start["pages"] = (self.app._progress_start["pages"][0] - 10, 0)
        self.app._progress(10, 100, "pages")            # 10 in 10 s: about 90 s left
        import time as _t
        left = self.app._eta_end - _t.monotonic()
        self.assertTrue(80 < left < 100, left)
        self.app._tick()
        self.assertIn("Reading Pages", self.app.working.get())
        self.assertIn("left", self.app.working.get())
        self.app._set_busy(False)
        self.assertEqual(self.app.working.get(), "")


class StaffRows(unittest.TestCase):
    TREE = [{"id": 445, "name": "Public Works", "path": []}, {"id": 446, "name": "Streets", "path": ["Public Works"]}]
    PEOPLE = [{"id": 1068, "dept": 446, "name": "Nguyen, Avery", "published": True},
              {"id": 1069, "dept": 445, "name": "Patel, Jordan", "published": False}]
    EXPORT = [{"First Name": "Avery", "Last Name": "Nguyen", "Department1": "Public Works", "Department2": "Streets", "Department3": "", "Title": "Supervisor",
               "Email": "avery.nguyen@example.gov", "Phone": "555-0100"}]

    def test_names_status_ids_and_details(self):
        from autocentral.sitecontent import add_staff_rows, set_staff_name_order, split_name
        self.assertEqual(split_name("Nguyen, Avery"), ("Avery", "Nguyen"))
        ms = ModuleScan("staff", "Staff Directory")
        add_staff_rows(ms, self.TREE, self.PEOPLE, self.EXPORT)
        a, j = ms.rows
        self.assertEqual((a.name, a.status, a.item_id, a.category_id), ("Nguyen, Avery", "published", 1068, 446))
        self.assertEqual((a.fields["Title"], a.fields["Email"]), ("Supervisor", "avery.nguyen@example.gov"))
        self.assertEqual((j.name, j.status), ("Patel, Jordan", "unpublished"))
        set_staff_name_order(ms, False)
        self.assertEqual(a.name, "Avery Nguyen")

    def test_window_option_on_by_default(self):
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(str(e))
        root.withdraw()
        try:
            from autocentral.gui import App
            from autocentral.sitecontent import add_staff_rows
            with mock.patch("autocentral.gui.save_settings"):
                app = App(root)
                app.settings.pop("staff_last_first", None)
                app.last_first.set(True)
                ms = ModuleScan("staff", "Staff Directory")
                add_staff_rows(ms, self.TREE, self.PEOPLE, self.EXPORT, last_first=False)
                app.set_module(ms, "staff")
                self.assertEqual(ms.rows[0].name, "Nguyen, Avery")
                app.last_first.set(False)
                app._staff_name_order()
                self.assertEqual(ms.rows[0].name, "Avery Nguyen")
                self.assertIn("published", app.tabs["staff"].show_vars)
        finally:
            root.destroy()


class StaffReadFix(unittest.TestCase):
    def test_departments_from_the_loader(self):
        from autocentral.scanner import tree_from_loader
        tree = tree_from_loader([{"id": 445, "name": "Public Works", "published": True, "parent": 0},
                                 {"id": 446, "name": "Streets", "published": False, "parent": 445},
                                 {"id": 447, "name": "Signs", "published": True, "parent": 446}])
        by = {d["id"]: d for d in tree}
        self.assertEqual(by[447]["path"], ["Public Works", "Streets"])
        self.assertEqual((by[446]["status"], by[446]["parent"]), ("unpublished", 445))
        self.assertIsNone(by[445]["parent"])

    def test_frames_with_no_answer_is_a_failed_job_not_a_crash(self):
        from autocentral.browser import Browser
        from autocentral.reporting import Reporter
        b = Browser("https://" + HOST, Reporter())
        with mock.patch.object(b, "_run_background", return_value=[None]):
            res = b.frames([{"src": "/admin/Directory.aspx"}, {"src": "/x"}])
        self.assertEqual([r["ok"] for r in res], [False, False])
        self.assertEqual(res[0]["pages"], [])


class TextBlocks(unittest.TestCase):
    BLOCK = {"id": "efc1", "html": '<p>Welcome to <a href="/10/Parks">Parks</a></p><p>Open daily.</p>',
             "text": "Welcome to Parks\nOpen daily.", "links": 1, "images": 0}

    def scan(self):
        from autocentral.sitecontent import mark_shared_blocks
        ms = ModuleScan("pages", "Pages")
        add_page_rows(ms, {"id": 1, "title": "Home", "status": "published", "path": [], "blocks": []}, "Home", HOST)
        ms.rows[0].extra["stylesheets"] = ["https://" + HOST + "/a.css"]
        add_page_rows(ms, {"id": 10, "title": "Parks", "status": "published", "parent": 1, "path": ["Home"],
                           "blocks": [self.BLOCK, {"id": "shared1", "html": "<p>Footer</p>", "text": "Footer"}]}, "Parks", HOST)
        add_page_rows(ms, {"id": 11, "title": "Trails", "status": "published", "parent": 1, "path": ["Home"],
                           "blocks": [{"id": "shared1", "html": "<p>Footer</p>", "text": "Footer"}]}, "Trails", HOST)
        mark_shared_blocks(ms)
        return ms

    def test_rows_and_shared(self):
        ms = self.scan()
        blocks = [r for r in ms.rows if r.status == "text block"]
        self.assertEqual(blocks[0].name, "Welcome to Parks")
        self.assertEqual(blocks[0].fields["Words"], "5")
        self.assertIn('<a href="/10/Parks">', blocks[0].fields["HTML"])
        self.assertNotIn("Shared", blocks[0].fields)
        self.assertEqual(blocks[1].fields["Shared"], "on 2 pages")

    def test_preview_page(self):
        from autocentral.sitecontent import preview_html
        out = preview_html("https://" + HOST, "Parks", [self.BLOCK["html"]], ["https://" + HOST + "/a.css"])
        self.assertIn('<base href="https://' + HOST + '/">', out)
        self.assertIn('<link rel="stylesheet" href="https://' + HOST + '/a.css">', out)
        self.assertIn('<div class="widget editor"><p>Welcome to', out)

    def test_window_panel_search_and_columns(self):
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(str(e))
        root.withdraw()
        try:
            from autocentral.gui import App
            app = App(root)
            app.set_module(self.scan(), "pages")
            tab = app.tabs["pages"]
            labels = [tab.tree.heading(c)["text"] for c in tab.tree["displaycolumns"]]
            self.assertNotIn("HTML", labels)
            self.assertNotIn("Text", labels)
            page = next(i for i, r in enumerate(tab.scan.rows) if r.category_id == 10 and r.level == "category")
            tab.tree.selection_set(str(page))
            tab.preview.show_selected()
            self.assertIn("Welcome to Parks", tab.preview.text.get("1.0", "end"))
            self.assertIn("<a href=", tab.preview.html.get("1.0", "end"))
            self.assertEqual(len(tab.preview.rows), 2)
            tab.f_text.set("Open daily")                    # the search looks inside block text
            names = {tab.scan.rows[i].name for i in tab.matched}
            self.assertEqual(names, {"Welcome to Parks"})
            with mock.patch("webbrowser.open") as wb:
                tab.preview.preview_in_browser()
            self.assertTrue(wb.call_args[0][0].startswith("file:"))
        finally:
            root.destroy()
