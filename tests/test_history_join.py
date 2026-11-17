"""Matching History Log rows to inventory rows."""

import unittest

from autocentral.historylog import join_history
from autocentral.inventory import RowFilter, select
from autocentral.models import HistoryEntry, InventoryRow


def entry(ids, when, who="A Person", action="Create"):
    return HistoryEntry(title="t", when=when, who=who, ids=ids, action=action)


class Join(unittest.TestCase):
    def test_categories_and_items(self):
        rows = [InventoryRow("archive", "category", "Board", "Board", category_id=38),
                InventoryRow("archive", "item", "Board", "Minutes", category_id=38, item_id=4400)]
        n = join_history(rows, [entry("AMID 38", "2015-01-01 10:00:00", "Ann"),
                                entry("AMID 38 ADID 4400", "2016-01-29 16:40:50", "Shari"),
                                entry("AMID 38 ADID 4400", "2017-02-01 09:00:00", "Bob", "Modify")])
        self.assertEqual(n, 2)
        self.assertEqual((rows[0].created_by, rows[0].created_on), ("Ann", "2015-01-01 10:00:00"))
        self.assertEqual((rows[1].created_by, rows[1].modified_by, rows[1].modified_on),
                         ("Shari", "Bob", "2017-02-01 09:00:00"))

    def test_category_id_never_taken_for_item_id(self):
        # Form Center: "CID 2" is category 2; form 2 in category 1 must not match it.
        rows = [InventoryRow("forms", "item", "Sample", "Board Form", category_id=1, item_id=2),
                InventoryRow("forms", "item", "Sample", "Other", category_id=1, item_id=5)]
        join_history(rows, [entry("CID 2", "2012-12-27 10:17:41"), entry("CID 1 FID 5", "2013-01-01 00:00:00")])
        self.assertEqual(rows[0].created_by, "")
        self.assertEqual(rows[1].created_on, "2013-01-01 00:00:00")

    def test_item_in_other_category_does_not_match(self):
        rows = [InventoryRow("forms", "item", "A", "x", category_id=1, item_id=7)]
        join_history(rows, [entry("CID 9 FID 7", "2013-01-01 00:00:00")])
        self.assertEqual(rows[0].created_by, "")

    def test_save_and_publish_counts_as_created(self):
        rows = [InventoryRow("archive", "item", "B", "x", category_id=122, item_id=4434)]
        join_history(rows, [entry("AMID 122 ADID 4434", "2025-03-05 10:11:30", "David", "Save And Publish")])
        self.assertEqual(rows[0].created_by, "David")

    def test_older_than_log_leaves_created_blank(self):
        rows = [InventoryRow("bids", "item", "B", "x", category_id=1, item_id=2)]
        join_history(rows, [entry("BCID 1 BID 2", "2019-01-01 00:00:00", "Z", "Modify")])
        self.assertEqual(rows[0].created_by, "")
        self.assertEqual(rows[0].modified_by, "Z")
        self.assertIn("history_starts", rows[0].extra)

    def test_flat_module_single_pair_items(self):
        rows = [InventoryRow("resdir", "item", "", "Shop", item_id=3084)]
        join_history(rows, [entry("BDID 3084", "2020-05-05 05:05:05", "Q")])
        self.assertEqual(rows[0].created_by, "Q")


class Filter(unittest.TestCase):
    def setUp(self):
        self.rows = [
            InventoryRow("bids", "item", "C", "Road bid", "published", created_on="2024-03-05 14:10:00",
                         created_by="Zed", module_label="Bid Postings"),
            InventoryRow("bids", "item", "C", "Old bid", "draft", created_on="2019-01-01 00:00:00",
                         created_by="Amy", module_label="Bid Postings"),
            InventoryRow("faq", "category", "Q", "Q", "published", module_label="FAQs"),
        ]

    def test_created_by_and_time_window(self):
        f = RowFilter(created_by=["zed"], created_from="2024-03-05 14:00", created_to="2024-03-05")
        self.assertEqual([r.name for r in select(self.rows, f)], ["Road bid"])

    def test_unknown_creator_and_module_label(self):
        self.assertEqual([r.name for r in select(self.rows, RowFilter(created_by=["unknown"]))], ["Q"])
        self.assertEqual(len(select(self.rows, RowFilter(modules=["Bid Postings"]))), 2)

    def test_text_and_regex(self):
        self.assertEqual(len(select(self.rows, RowFilter(text="bid"))), 2)
        self.assertEqual([r.name for r in select(self.rows, RowFilter(text="^Old", regex=True))], ["Old bid"])


if __name__ == "__main__":
    unittest.main()
