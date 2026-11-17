"""The window builds, shows scan results and filters them (no browser)."""

import tkinter as tk
import unittest

from autocentral.models import InventoryRow, ModuleScan


class Window(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:      # no display
            self.skipTest(str(e))
        self.root.withdraw()
        from autocentral.gui import App
        self.app = App(self.root)

    def tearDown(self):
        self.root.destroy()

    def test_rows_filter_and_sort(self):
        ms = ModuleScan("bids", "Bid Postings", rows=[
            InventoryRow("bids", "category", "Roads", "Roads", "published", category_id=1, module_label="Bid Postings"),
            InventoryRow("bids", "item", "Roads", "Paving", "draft", category_id=1, item_id=7,
                         created_on="2024-03-05 10:00:00", created_by="Zed", module_label="Bid Postings"),
        ])
        self.app._add_scan(ms)
        self.assertEqual(len(self.app.tree.get_children()), 2)
        self.app.f_level.set("item")
        self.app.refresh()
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.app.clear_filters()
        self.app.sort_by("name")
        self.assertEqual(self.app.shown[0].name, "Paving")
        self.assertIn("Showing 2 of 2", self.app.count.get())


if __name__ == "__main__":
    unittest.main()
