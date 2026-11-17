"""Parsers against saved copies of real training19 pages (tests/pages)."""

import unittest
from pathlib import Path

from autocentral import parse

PAGES = Path(__file__).parent / "pages"


def page(name: str) -> str:
    return (PAGES / f"{name}.html").read_text(encoding="utf-8")


class ModulePages(unittest.TestCase):
    def test_module_ids(self):
        for name, mid in [("agenda", 65), ("archive", 15), ("bids", 16), ("calendar", 58), ("facilities", 79),
                          ("faq", 25), ("formcenter", 71), ("resdir", 12), ("newsflash", 1), ("staff", 4)]:
            self.assertEqual(parse.module_id_of(page(name)), mid, name)

    def test_titles(self):
        self.assertEqual(parse.page_title(page("bids")), "Bid Postings")
        self.assertEqual(parse.page_title(page("resdir")), "Community Resource Directory")


class Classic(unittest.TestCase):
    def test_bid_categories(self):
        rows = parse.classic_rows(page("bids"))
        self.assertEqual([(r["id"], r["name"], parse.classic_status(r)) for r in rows],
                         [(69, "Example of Bid Opportunity", "published")])
        self.assertEqual(rows[0]["cols"]["Bid Count"], "0")

    def test_archive_categories_published_and_draft(self):
        rows = {r["id"]: r for r in parse.classic_rows(page("archive"))}
        self.assertEqual(parse.classic_status(rows[122]), "draft")
        self.assertEqual(parse.classic_status(rows[49]), "published")
        self.assertEqual(rows[49]["cols"]["Last Modified By"], "Heidi Rice")

    def test_archive_items(self):
        rows = parse.classic_rows(page("archive_list"))
        self.assertEqual(len(rows), 15)
        first = rows[0]
        self.assertEqual((first["id"], first["name"], parse.classic_status(first)),
                         (4447, "03-21-24 board_of_health", "draft"))
        self.assertEqual(sum(1 for r in rows if parse.classic_status(r) == "published"), 8)

    def test_faq_items_use_checkbox_id(self):
        rows = parse.classic_rows(page("faq_list"), id_arg=1)
        self.assertEqual([r["id"] for r in rows], [1266, 1265, 1264])

    def test_faq_categories_both_tables(self):
        rows = parse.classic_rows(page("faq"))
        self.assertEqual(len(rows), 20)
        self.assertEqual(sum(1 for r in rows if r["section"] == "published"), 10)

    def test_news_item_ignores_numbers_in_quoted_date(self):
        rows = parse.classic_rows(page("newsflash_list"))
        self.assertEqual([(r["id"], r["name"], parse.classic_status(r)) for r in rows],
                         [(871, "News Headline", "draft")])
        self.assertEqual(rows[0]["cols"]["Start Date"], "04/11/2016")

    def test_resource_directory_flat_list(self):
        rows = parse.classic_rows(page("resdir"))
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["name"], "11th Annual Plein Arts Festival")

    def test_calendars(self):
        cals = parse.calendar_list(page("calendar"))
        self.assertEqual([(c["id"], c["name"], c["events"]) for c in cals],
                         [(35, "County Holidays", 56), (14, "Main Calendar", 0), (58, "Training Calendar", 0)])


class Quirks(unittest.TestCase):
    def test_calendar_events_page(self):
        d = parse.calendar_events(page("calendar_events"))
        self.assertEqual((len(d["rows"]), d["total"], len(d["all_ids"]), d["max_page"]), (25, 56, 56, 3))
        self.assertIn("Pager_1", d["pager"])
        first = d["rows"][0]
        self.assertEqual((first["id"], first["name"], first["status"], first["past"]),
                         (217, "Presidents Day", "published", True))
        self.assertTrue(first["when"].startswith("Feb 17, 2014, 8:00 AM"))
        self.assertNotIn("�", first["when"])

    def test_calendar_view_options(self):
        opts = parse.calendar_view_options(page("calendar_events"))
        self.assertTrue(opts["Previous"])
        self.assertFalse(opts["ShowMineOnly"])

    def test_resource_directory_totals(self):
        self.assertEqual(parse.resdir_totals(page("resdir")), {1: 403})

    def test_staff_tree(self):
        tree = {d["id"]: d for d in parse.staff_tree(page("staff_expanded"))}
        self.assertEqual(len(tree), 11)
        self.assertEqual(tree[423]["status"], "unpublished")
        self.assertEqual(tree[424]["path"], ["Test", "sdfsdafasdf"])
        self.assertEqual(tree[422]["parent"], None)

    def test_staff_export(self):
        data = (PAGES / "staff_export.xlsx").read_bytes()
        e = parse.staff_export(data)
        self.assertEqual(len(e["departments"]), 11)
        self.assertEqual(e["departments"][3]["Sub-Department1"], "Test")


class Newer(unittest.TestCase):
    def test_categories(self):
        agenda = parse.newer_categories(page("agenda"))
        self.assertEqual(len(agenda), 5)
        self.assertIn("Agendas: 81", agenda[0]["detail"])
        forms = {c["id"]: c for c in parse.newer_categories(page("formcenter"))}
        self.assertEqual(forms[1]["status"], "draft")
        self.assertEqual(forms[59]["status"], "published")
        self.assertEqual(len(parse.newer_categories(page("facilities"))), 3)

    def test_forms(self):
        data = parse.form_list(page("fc_cat"))
        self.assertEqual(data["max_page"], 2)
        self.assertEqual(data["rows"][0], {"id": 1, "name": "Beautification Nomination Form", "status": "draft",
                                           "submissions": "0", "modified": "2012-01-28"})

    def test_facilities_include_subfacilities(self):
        rows = parse.facility_list(page("fac_cat"))["rows"]
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["name"], "Avalon Park")


class History(unittest.TestCase):
    def test_page(self):
        data = parse.history_page(page("hl_archive_create"))
        self.assertTrue(data["found_table"])
        self.assertEqual(len(data["entries"]), 100)
        self.assertEqual(data["max_page"], 12)
        e = data["entries"][1]
        self.assertEqual((e.when, e.who, e.ids, e.action), ("2016-01-29 16:40:50", "Shari Ireton",
                                                            "AMID 38 ADID 4400", "Create"))
        self.assertEqual(e.id_pairs, [("AMID", 38), ("ADID", 4400)])

    def test_people_and_dates(self):
        self.assertEqual(parse.person("Hazelwood, David"), "David Hazelwood")
        self.assertEqual(parse.person("City of Buffalo"), "City of Buffalo")
        self.assertEqual(parse.site_date("3/4/2025 11:25:52 AM"), "2025-03-04 11:25:52")
        self.assertEqual(parse.site_date("Jan 28, 2012"), "2012-01-28")


if __name__ == "__main__":
    unittest.main()
