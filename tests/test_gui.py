"""The window builds, shows a module as a tree, filters, sorts and ticks (no browser)."""

import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from autocentral.models import InventoryRow, ModuleScan
from autocentral.store import load_scan, save_scan


def bids_scan() -> ModuleScan:
    return ModuleScan("bids", "Bid Postings", read_at="2026-11-17 09:00", rows=[
        InventoryRow("bids", "category", "Roads", "Roads", "published", category_id=1),
        InventoryRow("bids", "item", "Roads", "Paving", "draft", category_id=1, item_id=7,
                     created_on="2024-03-05 10:00:00", created_by="Zed", fields={"Closing date": "2024-04-01"}),
        InventoryRow("bids", "item", "Roads", "Bridge", "published", category_id=1, item_id=8,
                     fields={"Closing date": "2023-01-01"}),
        InventoryRow("bids", "category", "Parks", "Parks", "published", category_id=2),
        InventoryRow("bids", "item", "Parks", "Mowing", "published", category_id=2, item_id=9,
                     created_on="2020-01-01 08:00:00", created_by="Amy"),
    ])


class Window(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:      # no display
            self.skipTest(str(e))
        self.root.withdraw()
        from autocentral.gui import App
        self.app = App(self.root)
        self.app.set_module(bids_scan(), "bids")
        self.tab = self.app.tabs["bids"]

    def tearDown(self):
        self.root.destroy()

    def test_tree_and_module_columns(self):
        t = self.tab.tree
        self.assertEqual(t.get_children(""), ("0", "3"))           # two categories
        self.assertEqual(t.get_children("0"), ("1", "2"))           # their items under them
        self.assertIn("f0", t["columns"])
        self.assertEqual(t.heading("f0")["text"], "Closing date")
        self.assertEqual(self.app.nb.tab(self.tab.frame, "text"), "Bid Postings")

    def test_created_filters_keep_no_info_rows(self):
        self.tab.f_from.set("2024-01-01")                 # Bridge has no created info
        self.assertEqual(self.tab.matched, {0, 1, 2, 3})  # Paving, plus the no-info rows; Mowing (2020) out
        self.tab.f_unknown.set(False)
        self.tab.refresh()
        self.assertEqual(self.tab.matched, {1})
        self.assertIn("context", self.tab.tree.item("0", "tags"))   # category shown greyed
        self.tab.clear_filters()
        self.tab.f_by.set("(no info)")
        self.tab.refresh()
        self.assertEqual(self.tab.matched, {0, 2, 3})

    def test_ticks_cascade_and_ticked_only(self):
        self.tab.toggle(["0"])
        self.assertEqual(self.tab.checked, {0, 1, 2})
        self.tab.toggle(["2"])
        self.assertEqual(self.tab.checked, {0, 1})
        self.tab.f_ticked.set(True)
        self.tab.refresh()
        self.assertEqual(self.tab.shown, [0, 1])
        self.assertIn("2 ticked", self.tab.count.get())

    def test_sort_within_category(self):
        self.tab.sort_by("f0")                            # Closing date, ascending
        self.assertEqual(self.tab.tree.get_children("0"), ("2", "1"))

    def test_read_button_flow(self):
        """Read this module -> worker -> tab updated and saved for the site."""
        import time
        from unittest import mock
        import autocentral.gui as g

        class FakeScanner:
            def __init__(self, browser, on_module_done=None, **kw):
                self.done = on_module_done

            def scan(self, keys, with_history=True):
                for k in keys:
                    ms = bids_scan()
                    ms.rows = ms.rows[:2]
                    self.done(ms)

        class FakeBrowser:
            host = "cp-training19.civicplus.com"
            r = None
            cancel = None

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(g, "Scanner", FakeScanner), \
                mock.patch.object(g, "cache_dir", lambda host: Path(d) / host):
            self.app.site.set("https://cp-training19.civicplus.com")
            self.app.worker.browser = FakeBrowser()
            self.app.read(["bids"])
            self.assertTrue(self.tab.reading)
            for _ in range(50):
                self.root.update()
                time.sleep(0.05)
                if not self.app.busy:
                    break
            self.assertFalse(self.app.busy)
            self.assertEqual(len(self.tab.scan.rows), 2)
            self.assertTrue(self.tab.scan.read_at)
            self.assertTrue((Path(d) / "cp-training19.civicplus.com" / "inventory_bids.json").exists())
            self.assertEqual(self.app.overview.tree.set("bids", "items"), "1")

    def test_saved_scan_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            save_scan(Path(d), bids_scan())
            ms = load_scan(Path(d), "bids")
        self.assertEqual(ms.read_at, "2026-11-17 09:00")
        self.assertEqual(ms.rows[1].fields, {"Closing date": "2024-04-01"})


if __name__ == "__main__":
    unittest.main()
