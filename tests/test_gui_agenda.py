"""Agenda Center actions in the window: compare, then dry run -> live (no browser)."""

import tempfile
import time
import tkinter as tk
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import openpyxl

from autocentral.models import InventoryRow, ModuleScan
from autocentral.reporting import ActionResult


def agenda_scan() -> ModuleScan:
    return ModuleScan("agenda", "Agenda Center", read_at="2026-11-17 12:00", rows=[
        InventoryRow("agenda", "category", "Council", "Council", "published", category_id=1),
        InventoryRow("agenda", "item", "Council", "Regular Meeting", "published", category_id=1, item_id=10,
                     fields={"Meeting date": "2024-01-02", "Minutes": "no"}),
        InventoryRow("agenda", "item", "Council", "Old", "published", category_id=1, item_id=11,
                     fields={"Meeting date": "2019-01-01", "Minutes": "no"}),
    ])


class FakeBrowser:
    host = "cp-training19.civicplus.com"
    r = None
    cancel = None


class FakeJobs:
    calls = []

    def __init__(self, browser, reporter, live):
        self.r, self.live = reporter, live

    def delete(self, sel):
        FakeJobs.calls.append(("delete", self.live, dict(sel.agendas)))
        self.r.result(ActionResult("delete", "delete", "agenda", "Council", "10",
                                   status="ok" if self.live else "dry-run"))


class AgendaWindow(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(str(e))
        self.root.withdraw()
        from autocentral.gui import App
        self.app = App(self.root)
        self.app.set_module(agenda_scan(), "agenda")
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

    def test_compare_window(self):
        from autocentral.gui_agenda import CompareWindow
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "AgendaCenter"
        ws.append(["Category", "Category Description", "Agenda Description", "Meeting Date",
                   "Minutes Available", "Agenda File Name", "Minutes File Name"])
        ws.append(["Council", None, "Regular Meeting", date(2024, 1, 2), True, "a.pdf", "m.pdf"])
        ws.append(["Council", None, "Regular Meeting", date(2024, 2, 6), False, "b.pdf", None])
        path = Path(self.tmp.name) / "import.xlsx"
        wb.save(path)
        with mock.patch("autocentral.gui_agenda.CompareWindow._save_settings"):
            w = CompareWindow(self.app)
            w.workbook.set(str(path))
            w.reread.set(False)
            w.run_compare()
        c = w.cmp.counts()
        self.assertEqual((c["missing"], c["missing minutes"], c["only on site"]), (1, 1, 1))
        self.assertEqual(str(w.btn_upload["state"]), "normal")
        self.assertIn("Missing (1)", w.nb.tab(0, "text"))

    def test_dry_run_then_live(self):
        from autocentral import gui_agenda
        tab = self.app.tabs["agenda"]
        tab.toggle(["1"])                                  # agenda 10
        FakeJobs.calls = []
        with mock.patch("autocentral.agendacenter.jobs.AgendaJobs", FakeJobs), \
                mock.patch.object(gui_agenda, "RUNS", Path(self.tmp.name)), \
                mock.patch("autocentral.gui_agenda.simpledialog.askstring", return_value="cp-training19.civicplus.com"):
            self.app.agenda_actions.ticked("delete")
            win = next(iter(self.app.run_windows.values()))
            self.pump()
            self.assertEqual(FakeJobs.calls, [("delete", False, {1: {10: "Regular Meeting"}})])
            self.assertEqual(str(win.btn_live["state"]), "normal")
            win.run_live()
            self.pump()
        self.assertEqual(FakeJobs.calls[-1][1], True)
        self.assertTrue((Path(self.tmp.name) / "cp-training19.civicplus.com").exists())
        self.assertIn("Done", win.state.get())

    def test_wrong_site_typed_changes_nothing(self):
        from autocentral import gui_agenda
        with mock.patch("autocentral.gui_agenda.simpledialog.askstring", return_value="cp-training18.civicplus.com"), \
                mock.patch("autocentral.gui_agenda.messagebox.showwarning"):
            self.assertFalse(gui_agenda.confirm_site(self.root, "cp-training19.civicplus.com", "Delete 1 agenda"))


if __name__ == "__main__":
    unittest.main()
