"""D10 dates, workbook-vs-site comparison and ticked-row selections (offline)."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import openpyxl

from autocentral.agendacenter.compare import (MISSING, MISSING_MINUTES, NO_CATEGORY, ON_SITE, ONLY_ON_SITE,
                                              SiteAgenda, compare, site_agendas)
from autocentral.agendacenter.d10 import local_time, pair_rows
from autocentral.agendacenter.importdata import ImportOptions, load_meetings
from autocentral.agendacenter.jobs import Selection
from autocentral.models import InventoryRow


def node(nid, title, epoch, agendas=(), minutes=(), packets=()):
    f = {"nid": [{"value": str(nid)}], "title": [{"value": title}],
         "field_smart_date": [{"value": str(epoch), "timezone": ""}]}
    for key, names in (("field_agendas", agendas), ("field_minutes_files", minutes), ("field_packets_files", packets)):
        if names:
            f[key] = [{"file_name": n, "file_url": f"https://old.example/{n}"} for n in names]
    return {"entity_id": str(nid), "is_published": True, "fields": f}


class D10Dates(unittest.TestCase):
    def test_local_time_daylight_saving(self):
        self.assertEqual(str(local_time(1514934000)), "2018-01-02 18:00:00")        # EST
        self.assertEqual(str(local_time(1530655200)), "2018-07-03 18:00:00")        # EDT
        self.assertEqual(str(local_time(1530655200, "Central")), "2018-07-03 17:00:00")
        self.assertEqual(str(local_time(1530655200, "America/Phoenix")), "2018-07-03 15:00:00")

    def test_workbook_without_dates(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "AgendaCenter"
            ws.append(["Category", "Category Description", "Agenda Description", "Meeting Date",
                       "Minutes Available", "Agenda File Name", "Minutes File Name"])
            ws.append(["Council", None, "Regular Meeting", None, True, "a1.pdf", "m1.pdf"])
            ws.append(["Council", None, "Work Session", None, False, None, None])       # complex
            ws.append(["Council", None, "No Meeting", None, False, None, None])         # complex, no files
            it = wb.create_sheet("AgendaCenterItems")
            it.append(["Name", "Category", "Meeting Date", "Meeting Time", "Description", "Is Default Item",
                       "File Name 1", "File Name 2"])
            it.append(["Agenda Packet", "Council", None, None, None, False, "packet2.pdf", None])
            it.append(["No Agenda or Minutes", "Council", None, None, None, False, None, None])
            wb.save(d / "import.xlsx")
            (d / "www-meetings.json").write_text(json.dumps([
                node(1, "Regular Meeting", 1514934000, ["a1.pdf"], ["m1.pdf"]),
                node(2, "Work Session", 1530655200, minutes=["m2.pdf"], packets=["packet2.pdf"]),
                node(3, "No Meeting", 1530741600),
            ]), encoding="utf-8")
            ms, warnings = load_meetings(ImportOptions(workbook=d / "import.xlsx",
                                                       d10_meetings=d / "www-meetings.json"))
        self.assertEqual(warnings, [])
        self.assertEqual([m.meeting_date for m in ms], [date(2018, 1, 2), date(2018, 7, 3), date(2018, 7, 4)])
        self.assertEqual(ms[1].kind, "complex")
        self.assertEqual(ms[1].items[0].files, ["packet2.pdf"])
        self.assertEqual(ms[1].attach_minutes_file, "m2.pdf")          # lost in the workbook, back from D10
        self.assertEqual(ms[1].file_urls["m2.pdf"], "https://old.example/m2.pdf")
        self.assertEqual(ms[2].items[0].name, "No Agenda or Minutes")
        self.assertTrue(any("by order" in n for n in ms[2].notes))

    def test_pairing_falls_back_to_file_names(self):
        from autocentral.agendacenter.d10 import D10File, D10Meeting
        ms = [D10Meeting(0, "1", "A", None, True, [D10File("agenda", "x.pdf", "")]),
              D10Meeting(1, "2", "B", None, True, [D10File("agenda", "y.pdf", "")])]
        rows = [{"Agenda Description": "B?", "Agenda File Name": "y.pdf"},
                {"Agenda Description": "A?", "Agenda File Name": "x.pdf"}]
        files = lambda r: [r["Agenda File Name"]]
        got = pair_rows(rows, ms, files)
        self.assertEqual({i: (m.nid, how) for i, (m, how) in got.items()},
                         {0: ("2", "file name"), 1: ("1", "file name")})


class Compare(unittest.TestCase):
    def meetings(self):
        from autocentral.agendacenter.models import ImportItem, Meeting
        return [
            Meeting("Council", date(2024, 1, 2), "Regular Meeting", "", agenda_file="a.pdf", minutes_file="m.pdf"),
            Meeting("Council", date(2024, 1, 2), "Special Meeting", "", agenda_file="s.pdf"),
            Meeting("Council", date(2024, 2, 6), "Regular Meeting", "", items=[ImportItem("Packet", ["p.pdf"])],
                    attach_minutes_file="m2.pdf"),
            Meeting("Council", date(2024, 3, 5), "Regular Meeting", "", agenda_file="c.pdf"),
            Meeting("Parks Board", date(2024, 1, 9), "Meeting", "", agenda_file="pb.pdf"),
        ]

    def test_outcomes(self):
        site = [SiteAgenda(11, 1, "Council", "Special Meeting", date(2024, 1, 2), "published", False),
                SiteAgenda(10, 1, "Council", "Regular Meeting", date(2024, 1, 2), "published", True),
                SiteAgenda(12, 1, "council", "Regular Meeting Agenda", date(2024, 2, 6), "draft", False),
                SiteAgenda(13, 1, "Council", "Old one", date(2019, 5, 1), "published", False)]
        cmp = compare(self.meetings(), site, ["Council"])
        got = [(r.outcome, r.site.id if r.site else None) for r in cmp.rows]
        self.assertEqual(got, [(ON_SITE, 10), (ON_SITE, 11), (MISSING_MINUTES, 12), (MISSING, None),
                               (NO_CATEGORY, None), (ONLY_ON_SITE, 13)])
        self.assertIn("draft", cmp.rows[2].detail)
        self.assertEqual(cmp.counts()[MISSING], 1)

    def test_category_map(self):
        cmp = compare(self.meetings()[4:], [], ["Parks & Recreation Board"],
                      category_map={"Parks Board": "Parks & Recreation Board"})
        self.assertEqual(cmp.rows[0].outcome, MISSING)
        self.assertEqual(cmp.rows[0].category, "Parks & Recreation Board")

    def test_site_agendas_from_inventory(self):
        rows = [InventoryRow("agenda", "category", "Council", "Council", "published", category_id=1),
                InventoryRow("agenda", "item", "Council", "Regular Meeting", "published", category_id=1,
                             item_id=10, fields={"Meeting date": "2024-01-02", "Minutes": "yes"})]
        site, cats = site_agendas(rows)
        self.assertEqual(cats, {1: "Council"})
        self.assertEqual((site[0].meeting_date, site[0].has_minutes), (date(2024, 1, 2), True))


class Selections(unittest.TestCase):
    def test_agendas_in_a_ticked_category_go_with_it(self):
        rows = [InventoryRow("agenda", "category", "A", "A", category_id=1),
                InventoryRow("agenda", "item", "A", "a1", category_id=1, item_id=10),
                InventoryRow("agenda", "item", "B", "b1", category_id=2, item_id=20)]
        sel = Selection.from_rows(rows)
        self.assertEqual(sel.categories, {1: "A"})
        self.assertEqual(sel.agendas, {2: {20: "b1"}})
        self.assertEqual(sel.describe(), "1 category and 1 agenda(s)")


if __name__ == "__main__":
    unittest.main()
