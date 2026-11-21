"""The D10 project add-on, offline: reading exports, page content, the plan
(tree order, files), address rewriting, the workbooks, tracking, the switch."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from autocentral.addons import enabled
from autocentral.addons.d10_project import content, workbook
from autocentral.addons.d10_project.exports import load
from autocentral.addons.d10_project.project import Options, plan, read_sitemap
from autocentral.addons.d10_project.tracking import write_tracking

HOST = "www.exampletown.org"


def node(nid, bundle, title, alias, extra=None, kind_field="nid", published=True):
    f = {kind_field: [{"value": str(nid)}], "title": [{"value": title}], "status": [{"value": "1"}],
         "path": [{"alias": alias}],
         "metatag": [{"tag": "link", "attributes": {"rel": "canonical", "href": f"https://{HOST}{alias}"}}]}
    f.update(extra or {})
    return {"entity_id": str(nid), "bundle": bundle, "is_published": str(published), "fields": repr(f)}


def media(mid, bundle, name, url, ms="2"):
    key = "field_media_image" if bundle == "image" else "field_media_file"
    f = {"mid": [{"value": str(mid)}], "name": [{"value": name}], "status": [{"value": "1"}],
         key: [{"file_url": url, "file_name": url.rsplit("/", 1)[-1], "alt": name}], "microsite": [{"target_id": ms}]}
    return {"entity_id": str(mid), "bundle": bundle, "is_published": "True", "fields": repr(f)}


def make_exports(folder: Path) -> None:
    w = lambda name, recs: (folder / f"www-{name}.json").write_text(json.dumps(recs), encoding="utf-8")
    w("microsites", [node(1, "microsite", "Home Page", "/home-page", {"field_synonym": [{"value": "home-page"}]}),
                     node(2, "microsite", "Police", "/police", {"body": [{"value": "<p>Police home</p>"}],
                                                                "field_phone_number": [{"value": "555-1234"}]})])
    w("pages", [
        node(10, "page", "Lost Animals", "/police/page/lost-animals",
             {"og_audience": [{"target_id": "2"}],
              "body": [{"value": '<p>See <a href="/media/50">the form</a> and <a href="/node/11">fees</a>.</p>'}],
              "field_sections": [{"target_id": 70}]}),
        node(11, "page", "Fees", "/police/page/fees", {"og_audience": [{"target_id": "2"}],
                                                      "field_file_attachments": [{"target_id": 51}]}),
        node(12, "page", "Notice", "/police/page/notice", {"og_audience": [{"target_id": "2"}],
                                                          "field_news_type": [{"value": "public_notice"}],
                                                          "field_news_date": "2024-06-27",
                                                          "body": [{"value": "<p>Road closed</p>"}]}),
        node(13, "page", "Picnic", "/police/page/picnic", {"og_audience": [{"target_id": "2"}],
                                                          "field_smart_date": [{"value": "1719500400", "end_value": "1719504000"}]}),
    ])
    w("paragraphs", [{"entity_id": "70", "bundle": "image_text", "is_published": "True", "fields": repr({
        "id": [{"value": "70"}], "field_image": [{"target_id": 52}], "field_text": [{"value": "<p>Our dog</p>"}],
        "field_position": [{"value": "left"}]})}])
    w("media", [media(50, "file", "Request Form", f"https://{HOST}/sites/files/form.pdf"),
                media(51, "file", "Fee Schedule", f"https://{HOST}/sites/files/a/form.pdf"),
                media(52, "image", "Dog", f"https://{HOST}/sites/files/dog.jpg")])
    w("directory_listings", [node(41, "directory_listing", "Ann Lee", "/police/directory-listing/ann-lee",
                                  {"field_first_name": [{"value": "Ann"}], "field_last_name": [{"value": "Lee"}],
                                   "field_position": [{"value": "Chief"}], "og_audience": [{"target_id": "2"}]})])
    w("faqs", [])


class D10Project(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ex = Path(self.tmp.name) / "exports"
        self.ex.mkdir()
        make_exports(self.ex)
        self.site = load(self.ex)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_exports_and_addresses(self):
        s = self.site
        self.assertEqual((s.host, s.name), (HOST, "exampletown"))
        self.assertEqual(s.node_for_url(f"https://{HOST}/police/page/fees").title, "Fees")
        self.assertEqual(s.node_for_url("/node/10").title, "Lost Animals")
        self.assertEqual(s.media_for_url("/media/50").title, "Request Form")
        self.assertEqual(s.media_for_url(f"https://{HOST}/sites/files/dog.jpg").id, "52")

    def test_page_html_has_sections_attachments_and_refs(self):
        b = content.build_html(self.site, self.site.nodes["10"])
        self.assertIn("Our dog", b.html)
        self.assertIn('src="/media/52"', b.html)
        self.assertEqual(set(b.media), {"50", "52"})
        self.assertEqual(b.nodes, ["11"])
        b2 = content.build_html(self.site, self.site.nodes["11"])
        self.assertIn("<h2>Documents</h2>", b2.html)

    def test_rewrite(self):
        html, changed = content.rewrite('<a href="/media/50">x</a><img src="/x.png">',
                                        lambda u: "/DocumentCenter/View/9" if u == "/media/50" else None)
        self.assertEqual(html, '<a href="/DocumentCenter/View/9">x</a><img src="/x.png">')
        self.assertEqual(changed, [("/media/50", "/DocumentCenter/View/9")])

    def test_plan_tree_files_and_modules(self):
        pr = plan(self.site, Path(self.tmp.name) / "work", Options(root_page="D10 Test"))
        titles = [p["title"] for p in pr.pages]
        self.assertEqual(titles[0], "D10 Test")
        self.assertLess(titles.index("Police"), titles.index("Lost Animals"))        # parents first
        self.assertNotIn("Notice", titles)                                             # news, not a page
        self.assertEqual([n["title"] for n in pr.news], ["Notice"])
        self.assertEqual([e["title"] for e in pr.events], ["Picnic"])
        self.assertEqual([s["last"] for s in pr.staff], ["Lee"])
        names = sorted(f["file"] for f in pr.files)
        self.assertEqual(len(set(names)), 3)                                           # two form.pdf: one renamed
        self.assertTrue(any(n.startswith("form_") for n in names))
        dog = pr.file("52")
        self.assertTrue(dog["image"])
        self.assertEqual((dog["root"], dog["folder"]), ("Exampletown Images", "Police"))
        self.assertTrue((Path(self.tmp.name) / "work" / "project.json").is_file())

    def test_workbooks(self):
        pr = plan(self.site, Path(self.tmp.name) / "work", Options())
        files_dir = Path(self.tmp.name) / "files"
        files_dir.mkdir()
        for f in pr.files:
            (files_dir / f["file"]).write_bytes(b"%PDF-1.4 test")
            f["download"], f["size"] = "ok", 13
        zips = workbook.write_files_batches(pr.files, files_dir, Path(self.tmp.name) / "b", pr.site)
        self.assertEqual(len(zips), 1)
        import io
        import openpyxl
        with zipfile.ZipFile(zips[0]) as z:
            self.assertIn(workbook.WORKBOOK_NAME, z.namelist())
            wb = openpyxl.load_workbook(io.BytesIO(z.read(workbook.WORKBOOK_NAME)))
        self.assertEqual(wb["DocumentCenter"].cell(1, 3).value, "Parent Folder Name")
        self.assertEqual(wb["DocumentCenter"].max_row, 3)
        self.assertEqual(wb["ImageRepository"].max_row, 2)
        rows = workbook.page_rows(pr.pages)
        self.assertTrue(all(r[4] == "Editor" for r in rows))
        self.assertEqual(rows[0][2], "Home")
        out = workbook.calendar_csv(pr.events, Path(self.tmp.name) / "cal.csv")
        self.assertIn("Picnic", out.read_text(encoding="utf-8-sig"))

    def test_sitemap_and_tracking(self):
        sm = Path(self.tmp.name) / "site-map.csv"
        sm.write_text('"Page Setting","Tier 1","Tier 2","Tier 3","Tier 4","Tier 5","Tier 6","Tier 7","Tier 8","Linked Page","URL"\n'
                      f'"","Government","","","","","","","","",""\n'
                      f'"","","Police","","","","","","","","https://{HOST}/police"\n'
                      f'"","","Chamber","","","","","","","x","https://chamber.example.com/"\n', encoding="utf-8")
        self.assertEqual([r["path"] for r in read_sitemap(sm)], [["Government"], ["Government", "Police"],
                                                                 ["Government", "Chamber"]])
        pr = plan(self.site, Path(self.tmp.name) / "work", Options(), sm)
        by = {p["key"]: p for p in pr.pages}
        police = next(p for p in pr.pages if p["title"] == "Police")
        self.assertEqual(by[police["parent"]]["title"], "Government")
        self.assertTrue(any(p["kind"] == "linked" and p["link_to"].startswith("https://chamber") for p in pr.pages))
        out = write_tracking(pr, Path(self.tmp.name) / "t.xlsx")
        import openpyxl
        self.assertEqual(openpyxl.load_workbook(out).sheetnames, ["Steps", "Pages", "Files", "Links", "Modules", "Check", "To do"])

    def test_switch(self):
        self.assertEqual(enabled({}), ["d10_project"])
        self.assertEqual(enabled({"addons": {"d10_project": False}}), [])


if __name__ == "__main__":
    unittest.main()


class Prompts(unittest.TestCase):
    def test_accepts_results_refuses_deletes(self):
        from autocentral.addons.d10_project.prompts import decide
        self.assertTrue(decide("Import results\n36 rows imported successfully."))
        self.assertTrue(decide("A phone number or email address wasn't provided ... Proceed?"))
        self.assertFalse(decide("Delete all existing entries?"))
        self.assertFalse(decide("This will remove every listing. Continue?"))


class Tab(unittest.TestCase):
    def test_tab_loads_in_the_window(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(str(e))
        root.withdraw()
        try:
            from autocentral.gui import App
            app = App(root)
            self.assertIn("d10_project", app.addons)          # a broken add-on is skipped silently: catch it here
            names = [app.nb.tab(t, "text") for t in app.nb.tabs()]
            self.assertIn("D10 project", names)
            self.assertEqual(names[-1], "Plan")
        finally:
            root.destroy()
