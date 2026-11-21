"""Publish / unpublish / delete / create for the modules other than Agenda
Center (autocentral.actions), with the in-page requests faked."""

import json
import shutil
import subprocess
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from autocentral.actions import engine
from autocentral.actions.jobs import ModuleJobs, Selection, same_person
from autocentral.models import InventoryRow, ModuleScan
from autocentral.modules import MODULES, VIEW_ONLY_KEYS
from autocentral.reporting import ActionResult, CancelToken, Reporter


class FakeSite:
    """A module on a pretend site: categories with items, and a log of the
    requests made, in order."""

    def __init__(self, cats, items):
        self.cats = {c["id"]: dict(c) for c in cats}
        self.items = {cid: {i["id"]: dict(i) for i in its} for cid, its in items.items()}
        self.calls = []
        self.refuse_delete = set()

    def __call__(self, mod, op, *args):
        self.calls.append((op,) + tuple(a if not isinstance(a, list) else tuple(a) for a in args))
        if op == "cats":
            return list(self.cats.values())
        if op == "items":
            return list(self.items.get(args[0], {}).values())
        if op == "catAct":
            action, ids = args[0], args[1]
            for i in ids:
                if action == "delete":
                    if i not in self.refuse_delete:
                        self.cats.pop(i, None)
                else:
                    self.cats[i]["published"] = action == "publish"
            return {"ok": list(ids), "failed": {}}
        if op == "itemAct":
            action, cid, ids = args
            for i in ids:
                if action == "delete":
                    self.items[cid].pop(i, None)
                else:
                    self.items[cid][i]["published"] = action == "publish"
            return {"ok": list(ids), "failed": {}}
        if op == "create":
            nid = max(self.cats) + 1
            self.cats[nid] = {"id": nid, "name": args[0], "published": args[1]}
            return {"id": nid}
        raise AssertionError(op)


class FakeBrowser:
    host = "cp-training19.civicplus.com"

    def __init__(self):
        self.r = None
        self.cancel = CancelToken()


def make_jobs(site, live, key="bids"):
    rep = Reporter()
    jobs = ModuleJobs(FakeBrowser(), rep, live, key)
    jobs.relay.call = site
    return jobs, rep


def statuses(rep):
    return [(r.action, r.target_type, r.target_id, r.status) for r in rep.results]


SITE = dict(cats=[{"id": 1, "name": "Roads", "published": True}, {"id": 2, "name": "Parks", "published": False}],
            items={1: [{"id": 10, "name": "Bid A", "published": True}, {"id": 11, "name": "Bid B", "published": False}],
                   2: [{"id": 20, "name": "Bid C", "published": False}]})


class SelectionTests(unittest.TestCase):
    def test_items_in_ticked_categories_belong_to_the_category(self):
        rows = [InventoryRow("bids", "category", "Roads", "Roads", category_id=1),
                InventoryRow("bids", "item", "Roads", "Bid A", category_id=1, item_id=10),
                InventoryRow("bids", "item", "Parks", "Bid C", category_id=2, item_id=20)]
        sel = Selection.from_rows("bids", rows)
        self.assertEqual(sel.categories, {1: "Roads"})
        self.assertEqual(sel.items, {2: {20: "Bid C"}})
        self.assertEqual(sel.describe(), "1 category and 1 bid")

    def test_resource_directory_and_staff_rows(self):
        sel = Selection.from_rows("resdir", [InventoryRow("resdir", "item", "", "Shop", item_id=5)])
        self.assertEqual(sel.items, {0: {5: "Shop"}})
        sel = Selection.from_rows("staff", [InventoryRow("staff", "item", "Police", "Ann Lee")])
        self.assertEqual(len(sel.unresolved), 1)
        self.assertEqual(sel.describe(), "1 employee")

    def test_same_person(self):
        self.assertTrue(same_person("Lee, Ann", "Ann  Lee"))
        self.assertFalse(same_person("Lee, Ann", "Ann Leigh"))


class JobTests(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        site = FakeSite(**SITE)
        jobs, rep = make_jobs(site, live=False)
        jobs.delete(Selection("bids", categories={1: "Roads"}, items={2: {20: "Bid C"}}))
        self.assertFalse([c for c in site.calls if c[0] in ("catAct", "itemAct")])
        self.assertEqual({r.status for r in rep.results}, {"dry-run"})
        detail = next(r.detail for r in rep.results if r.target_id == "1")
        self.assertIn("first delete its 2 bid(s) (1 published: unpublished first)", detail)

    def test_delete_category_clears_it_first_then_checks_once(self):
        site = FakeSite(**SITE)
        jobs, rep = make_jobs(site, live=True)
        jobs.delete(Selection("bids", categories={1: "Roads"}))
        acts = [c for c in site.calls if c[0] in ("catAct", "itemAct")]
        self.assertEqual(acts, [("itemAct", "unpublish", 1, (10,)), ("itemAct", "delete", 1, (10, 11)),
                                ("catAct", "unpublish", (1,)), ("catAct", "delete", (1,))])
        self.assertNotIn(1, site.cats)
        self.assertEqual(statuses(rep), [("delete", "category", "1", "ok")])
        self.assertIn("with its 2 bid(s)", rep.results[0].detail)
        # one read before, one read at the end
        self.assertEqual([c[0] for c in site.calls].count("cats"), 2)
        self.assertEqual(rep.undo, [])           # deletes can't be undone

    def test_category_the_site_keeps_is_reported(self):
        site = FakeSite(**SITE)
        site.refuse_delete.add(2)
        jobs, rep = make_jobs(site, live=True)
        jobs.delete(Selection("bids", categories={2: "Parks"}))
        self.assertEqual(rep.results[-1].status, "error")
        self.assertIn("still on the site", rep.results[-1].detail)

    def test_publish_category_publishes_its_drafts_and_records_undo(self):
        site = FakeSite(**SITE)
        jobs, rep = make_jobs(site, live=True)
        jobs.set_published(Selection("bids", categories={2: "Parks"}), True)
        self.assertTrue(site.cats[2]["published"])
        self.assertTrue(site.items[2][20]["published"])
        self.assertEqual(sorted((u["level"], u["id"], u["undo"]) for u in rep.undo),
                         [("category", 2, "unpublish"), ("item", 20, "unpublish")])
        self.assertTrue(all(r.status == "ok" for r in rep.results))
        self.assertIn("Don't Send", next(r.detail for r in rep.results if r.target_type == "bid"))

    def test_unpublish_skips_what_is_already_unpublished_and_undo_puts_it_back(self):
        site = FakeSite(**SITE)
        jobs, rep = make_jobs(site, live=True)
        jobs.set_published(Selection("bids", items={1: {10: "Bid A", 11: "Bid B"}}), False)
        self.assertEqual(statuses(rep), [("unpublish", "bid", "11", "skipped"), ("unpublish", "bid", "10", "ok")]
                         if rep.results[0].target_id == "11" else
                         [("unpublish", "bid", "10", "ok"), ("unpublish", "bid", "11", "skipped")])
        self.assertFalse(site.items[1][10]["published"])
        jobs2, rep2 = make_jobs(site, live=True)
        jobs2.undo(rep.undo)
        self.assertTrue(site.items[1][10]["published"])
        self.assertEqual([r.status for r in rep2.results], ["ok"])

    def test_delete_everything(self):
        site = FakeSite(**SITE)
        jobs, rep = make_jobs(site, live=True)
        jobs.delete_everything()
        self.assertEqual(site.cats, {})

    def test_staff_department_goes_with_its_subdepartments_deepest_first(self):
        site = FakeSite(cats=[{"id": 1, "name": "Police", "published": True, "parent": 0},
                              {"id": 2, "name": "Patrol", "published": True, "parent": 1},
                              {"id": 3, "name": "Night", "published": False, "parent": 2},
                              {"id": 4, "name": "Fire", "published": True, "parent": 0}],
                        items={1: [], 2: [{"id": 7, "name": "Lee, Ann", "published": True}], 3: [], 4: []})
        jobs, rep = make_jobs(site, live=True, key="staff")
        jobs.delete(Selection("staff", categories={1: "Police"}))
        deletes = [c for c in site.calls if c[0] == "catAct" and c[1] == "delete"]
        self.assertEqual(deletes[0][2], (3, 2, 1))
        self.assertEqual(set(site.cats), {4})

    def test_staff_employee_without_id_is_found_by_name(self):
        site = FakeSite(cats=[{"id": 1, "name": "Police", "published": True, "parent": 0}],
                        items={1: [{"id": 7, "name": "Lee, Ann", "published": True}]})
        jobs, rep = make_jobs(site, live=True, key="staff")
        jobs.delete(Selection("staff", unresolved=[InventoryRow("staff", "item", "Police", "Ann Lee")]))
        self.assertEqual(site.items[1], {})

    def test_create_skips_existing_names_and_limits_calendar_names(self):
        site = FakeSite(**SITE)
        jobs, rep = make_jobs(site, live=True)
        jobs.create_categories(["roads", "Trails", "Trails"], True)
        self.assertEqual([r.status for r in rep.results], ["skipped", "skipped", "ok"])
        self.assertEqual(rep.undo[0]["undo"], "delete")
        jobs, rep = make_jobs(FakeSite(**SITE), live=False, key="calendar")
        jobs.create_categories(["x" * 51], True)
        self.assertEqual(rep.results[0].status, "error")


class EngineTests(unittest.TestCase):
    def test_every_module_but_agenda_has_a_relay_handler(self):
        self.assertEqual(set(engine.RELAY_KEY), {m.key for m in MODULES} - {"agenda"} - VIEW_ONLY_KEYS)
        for key in engine.RELAY_KEY.values():
            self.assertIn(f"{key}", engine.LIB)

    def test_library_version_is_its_hash(self):
        self.assertNotIn("__VERSION__", engine.LIB)

    def test_never_sends(self):
        # The values the Notify Me pop-up's Don't Send posts.
        self.assertIn("ysnSend: 'false', ysnSendAtStart: 'false', ysnSendBeforeDays: 'false'", engine.LIB)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_relay_js_parses(self):
        subprocess.run(["node", "--check", str(Path(engine.__file__).with_name("relay.js"))], check=True)


class FakeJobs:
    calls = []

    def __init__(self, browser, reporter, live, key):
        self.r, self.live, self.key = reporter, live, key

    def delete(self, sel):
        FakeJobs.calls.append(("delete", self.key, self.live, dict(sel.items)))
        self.r.result(ActionResult("delete", "delete", "bid", "Roads", "10",
                                   status="ok" if self.live else "dry-run"))


class WindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(str(e))
        self.root.withdraw()
        from autocentral.gui import App
        self.app = App(self.root)
        self.app.set_module(ModuleScan("bids", "Bid Postings", read_at="2026-11-20 10:00", rows=[
            InventoryRow("bids", "category", "Roads", "Roads", "published", category_id=1),
            InventoryRow("bids", "item", "Roads", "Bid A", "published", category_id=1, item_id=10)]), "bids")
        self.app.site.set("https://cp-training19.civicplus.com")
        self.app.worker.browser = FakeBrowser()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()
        self.root.destroy()

    def pump(self):
        for _ in range(60):
            self.root.update()
            time.sleep(0.03)
            if not self.app.busy:
                break
        self.root.update()

    def test_every_module_tab_has_the_actions(self):
        self.assertEqual(set(self.app.module_actions), {m.key for m in MODULES} - {"agenda"} - VIEW_ONLY_KEYS)
        texts = {b["text"] for b in self.app.module_actions["news"].buttons}
        self.assertTrue({"Publish ticked…", "Unpublish ticked…", "Delete ticked…",
                         "Delete everything in this module…", "Create categories…", "Undo a live run…"} <= texts)
        self.assertNotIn("Create categories…", {b["text"] for b in self.app.module_actions["resdir"].buttons})
        self.assertIn("Delete everything in this module…", {b["text"] for b in self.app.agenda_actions.buttons})

    def test_dry_run_then_live_for_a_module(self):
        from autocentral import gui_agenda
        tab = self.app.tabs["bids"]
        tab.toggle(["1"])                                   # Bid A
        FakeJobs.calls = []
        with mock.patch("autocentral.gui_actions.ModuleJobs", FakeJobs), \
                mock.patch.object(gui_agenda, "RUNS", Path(self.tmp.name)), \
                mock.patch("autocentral.gui_agenda.simpledialog.askstring", return_value="cp-training19.civicplus.com"):
            self.app.module_actions["bids"].ticked("delete")
            win = next(iter(self.app.run_windows.values()))
            self.assertEqual(win.module, "bids")
            self.pump()
            self.assertEqual(FakeJobs.calls, [("delete", "bids", False, {1: {10: "Bid A"}})])
            win.run_live()
            self.pump()
        self.assertEqual(FakeJobs.calls[-1][2], True)
        self.assertIn("Done", win.state.get())


if __name__ == "__main__":
    unittest.main()


class DocumentCenterAndWipe(unittest.TestCase):
    def test_top_folders_are_never_changed_and_subfolders_go_first(self):
        from autocentral.actions.jobs import ModuleJobs, Selection
        from autocentral.reporting import Reporter

        class FakeRelay:
            calls = []
            seconds = 0.0

            def __init__(self, b):
                pass

            def cats(self, mod):
                return [{"id": 1, "name": "Document Center", "parent": 0, "published": None, "top": True},
                        {"id": 10, "name": "Parks", "parent": 1, "published": None},
                        {"id": 11, "name": "Maps", "parent": 10, "published": None}]

            def items(self, mod, cid):
                return [{"id": 500, "name": "map.pdf", "published": True}] if cid == 11 else []

            def act(self, mod, level, action, ids, cid=0, extra=None):
                FakeRelay.calls.append((level, action, list(ids)))
                return list(ids), {}

        class B:
            host = "x"
            r = None

        from unittest import mock
        with mock.patch("autocentral.actions.jobs.Relay", FakeRelay):
            rep = Reporter()
            ModuleJobs(B(), rep, False, "docs").delete(Selection("docs", categories={1: "Document Center"}))
            self.assertEqual(rep.results[0].status, "skipped")
            rep = Reporter()
            j = ModuleJobs(B(), rep, True, "docs")
            j._check = lambda c, i: set(c)
            j.delete(Selection("docs", categories={10: "Parks"}))
        # the file is deleted as it is (no unpublish first), then Maps before Parks
        self.assertEqual(FakeRelay.calls, [("item", "delete", [500]), ("category", "delete", [11, 10])])

    def test_wipe_order_and_parts(self):
        from autocentral.actions.wipe import ORDER
        self.assertEqual(ORDER[0], "agenda")
        self.assertEqual(ORDER[-2:], ["pages", "docs"])
        self.assertEqual(len(ORDER), 12)
