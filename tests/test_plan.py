"""Find and replace rules, the Plan (steps, dry run, run, undo) with the site
faked, the Plan tab, and stepping through page previews."""

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from autocentral.actions.jobs import Selection
from autocentral.editing import Rule, apply_rule, find_matches
from autocentral.models import ModuleScan
from autocentral.plan_steps import (PlanJobs, Step, load_plan, module_step, move_pages_step, move_staff_step,
                                    replace_step, save_plan)
from autocentral.reporting import CancelToken, Reporter
from autocentral.sitecontent import add_page_rows, preview_pages_html

HOST = "cp-training19.civicplus.com"


class Rules(unittest.TestCase):
    def test_text_mode_leaves_tags_and_addresses_alone(self):
        html = '<p>The recieve desk: <a href="/recieve">recieve forms</a> &amp; more</p>'
        new, n = apply_rule(html, Rule("recieve", "receive"))
        self.assertEqual(n, 2)
        self.assertIn('href="/recieve"', new)
        self.assertIn("The receive desk", new)
        self.assertIn("&amp; more", new)

    def test_html_mode_changes_addresses(self):
        new, n = apply_rule('<a href="/UserFiles/a.pdf">a</a>', Rule("/UserFiles/", "/DocumentCenter/", in_html=True))
        self.assertEqual((n, new), (1, '<a href="/DocumentCenter/a.pdf">a</a>'))

    def test_whole_word_case_and_ampersand(self):
        self.assertEqual(apply_rule("<p>Parks and Parkside</p>", Rule("park", "Park", whole_word=True))[1], 0)
        self.assertEqual(apply_rule("<p>Parks and Parkside</p>", Rule("Parks", "Trails", whole_word=True, match_case=True)),
                         ("<p>Trails and Parkside</p>", 1))
        self.assertEqual(apply_rule("<p>Fish &amp; Game</p>", Rule("Fish & Game", "Wildlife")), ("<p>Wildlife</p>", 1))


def pages_scan():
    ms = ModuleScan("pages", "Pages")
    add_page_rows(ms, {"id": 1, "title": "Home", "status": "published", "path": []}, "Home", HOST)
    add_page_rows(ms, {"id": 10, "title": "Parks", "status": "published", "parent": 1, "path": ["Home"],
                       "blocks": [{"id": "b1", "html": "<p>Teh pool opens in May.</p>", "text": "Teh pool opens in May."}]},
                  "Parks", HOST)
    add_page_rows(ms, {"id": 11, "title": "Trails", "status": "draft", "parent": 10, "path": ["Home", "Parks"],
                       "blocks": [{"id": "b2", "html": "<p>Maps of teh trails.</p>", "text": "Maps of teh trails."}]},
                  "Trails", HOST)
    return ms


class Finding(unittest.TestCase):
    def test_matches_with_context_and_scope(self):
        ms = pages_scan()
        rule = Rule("teh", "the", whole_word=True)
        found = find_matches(ms.rows, rule)
        self.assertEqual([(m.page, m.block_id, m.count) for m in found], [("Parks", "b1", 1), ("Trails", "b2", 1)])
        self.assertIn("the pool", found[0].after)
        self.assertEqual(len(find_matches(ms.rows, rule, {11})), 1)


class FakeRelay:
    def __init__(self):
        self.calls = []

    def call(self, mod, op, *args):
        self.calls.append((mod, op) + args)
        return {"ok": True}


class FakeBrowser:
    host = HOST

    def __init__(self):
        self.r = None
        self.cancel = CancelToken()


class PlanRun(unittest.TestCase):
    def steps(self):
        return [move_pages_step([{"id": 11, "name": "Trails", "from": 10, "to": 1, "to_name": "Home"}], "Home"),
                move_staff_step([{"kind": "employee", "id": 7, "name": "Lee, Ann", "from": 2, "to": 3, "to_name": "Fire"},
                                 {"kind": "department", "id": 4, "name": "Patrol", "from": 2, "to": 3, "to_name": "Fire"}], "Fire")]

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "plan.json"
            st = self.steps() + [module_step("faq", "FAQs", "delete", Selection("faq", categories={5: "Old"}))]
            save_plan(p, st)
            back = load_plan(p)
        self.assertEqual([s.kind for s in back], ["move_pages", "move_staff", "module_action"])
        self.assertEqual(back[2].params["categories"], {"5": "Old"})

    def test_dry_run_changes_nothing_live_run_moves_and_records_undo(self):
        relay = FakeRelay()
        with mock.patch("autocentral.actions.engine.Relay", return_value=relay):
            rep = Reporter()
            PlanJobs(FakeBrowser(), rep, False).run(self.steps())
            self.assertEqual(relay.calls, [])
            self.assertEqual({r.status for r in rep.results}, {"dry-run"})
            rep = Reporter()
            PlanJobs(FakeBrowser(), rep, True).run(self.steps())
        self.assertEqual(relay.calls, [("pages", "move", 11, 1), ("staff", "moveEmployee", 7, 2, 3),
                                       ("staff", "moveDept", 4, 3)])
        self.assertEqual([u["to"] for u in rep.undo], [10, 2, 2])          # where each one came from
        relay2 = FakeRelay()
        with mock.patch("autocentral.actions.engine.Relay", return_value=relay2):
            PlanJobs(FakeBrowser(), Reporter(), True).undo(rep.undo)
        self.assertEqual(relay2.calls, [("pages", "move", 11, 10), ("staff", "moveEmployee", 7, 3, 2),
                                        ("staff", "moveDept", 4, 2)])

    def test_module_step_runs_the_module_job(self):
        st = module_step("faq", "FAQs", "delete", Selection("faq", categories={5: "Old"}))
        with mock.patch("autocentral.plan_steps.ModuleJobs") as MJ:
            PlanJobs(FakeBrowser(), Reporter(), True).run([st])
        sel = MJ.return_value.delete.call_args[0][0]
        self.assertEqual(sel.categories, {5: "Old"})

    def test_replace_step_dry_run(self):
        st = replace_step(Rule("teh", "the"), {10: {"name": "Parks", "published": True, "blocks": ["b1"]}})
        rep = Reporter()
        PlanJobs(FakeBrowser(), rep, False).run([st])
        self.assertEqual(rep.results[0].status, "dry-run")
        self.assertIn("publish again", rep.results[0].detail)


class Window(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(str(e))
        self.root.withdraw()
        self.tmp = tempfile.TemporaryDirectory()
        import autocentral.gui_plan as gp
        self.p = mock.patch.object(gp, "CURRENT", Path(self.tmp.name) / "current.json")
        self.p.start()
        from autocentral.gui import App
        self.app = App(self.root)

    def tearDown(self):
        self.p.stop()
        self.root.destroy()
        self.tmp.cleanup()

    def test_plan_tab_list(self):
        plan = self.app.plan
        names = [self.app.nb.tab(t, "text") for t in self.app.nb.tabs()]
        self.assertEqual(names[-1], "Plan")
        a, b = PlanRun().steps()
        plan.add(a)
        plan.add(b)
        self.assertEqual(self.app.nb.tab(plan.frame, "text"), "Plan (2)")
        plan.tree.selection_set(b.id)
        plan.move(-1)
        self.assertEqual([s.kind for s in plan.steps], ["move_staff", "move_pages"])
        plan.tree.selection_set(a.id)
        plan.remove()
        self.assertEqual([s.kind for s in plan.steps], ["move_staff"])

    def test_add_ticked_to_plan_from_a_module_tab(self):
        from autocentral.models import InventoryRow
        self.app.set_module(ModuleScan("faq", "FAQs", rows=[
            InventoryRow("faq", "category", "Old", "Old", "published", category_id=5)]), "faq")
        self.app.tabs["faq"].toggle(["0"])
        self.app.module_actions["faq"].to_plan("delete")
        st = self.app.plan.steps[-1]
        self.assertEqual((st.kind, st.params["action"], st.params["categories"]), ("module_action", "delete", {"5": "Old"}))

    def test_arrows_step_through_pages(self):
        self.app.set_module(pages_scan(), "pages")
        tab = self.app.tabs["pages"]
        tab.preview.step(1)
        first = tab.scan.rows[int(tab.tree.selection()[0])]
        tab.preview.step(1)
        second = tab.scan.rows[int(tab.tree.selection()[0])]
        self.assertEqual((first.name, second.name), ("Home", "Parks"))
        self.assertIn("Teh pool", tab.preview.text.get("1.0", "end"))
        tab.preview.step(-1)
        self.assertEqual(tab.scan.rows[int(tab.tree.selection()[0])].name, "Home")

    def test_multi_page_preview(self):
        out = preview_pages_html("https://" + HOST, [{"title": "Parks", "status": "published", "path": "Home", "url": "/10/Parks",
                                                       "blocks": ["<p>x</p>"]}], [])
        self.assertIn("ArrowRight", out)
        self.assertIn('"title": "Parks"', out)


if __name__ == "__main__":
    unittest.main()


class SameWords(unittest.TestCase):
    def test_page_scripts_dont_count(self):
        from autocentral.editing import plain
        shown = '<p>Hi  there</p><script>$(document).ready(function () { renderSlideshowIfApplicable(); });</script>'
        self.assertEqual(plain(shown), plain("<p>Hi there</p>"))
        self.assertNotEqual(plain("<p>Hi there</p>"), plain("<p>Hi here</p>"))
