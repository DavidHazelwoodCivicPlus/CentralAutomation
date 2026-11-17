"""Offline tests: no browser, no site. Run with:  python -m unittest -v"""

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import openpyxl

from autocentral.browser import looks_like_send_control
from autocentral.agendacenter.filters import AgendaFilter, ItemFilter
from autocentral.agendacenter.importdata import ImportOptions, find_import_files, is_duplicate, load_meetings, name_for
from autocentral.agendacenter.models import Agenda, AgendaDetails, Category, ImportItem, Item, Meeting
from autocentral.agendacenter.plan import Plan, Step, delete_contents_first

REAL_WB = Path.home() / "Desktop/Selenium Agenda Files for Testing/talbotcountyga-agenda-center-import/talbotcountyga-Master_Content_Import.xlsx"
REAL_JSON = Path.home() / "Desktop/Selenium Agenda Files for Testing/talbotcountyga-agenda-center-post-import-tasks.json"
REAL_ATTACH = Path.home() / "Desktop/Selenium Agenda Files for Testing/talbotcountyga-agenda-center-attach-after-import"


def agenda(**kw):
    base = dict(id=1, category_id=39, category_name="Bikeways Committee", meeting_date=date(2024, 6, 4),
                updated=date(2024, 6, 4), description="Bikeways Committee Regular Meeting Agenda (PDF)",
                published=True, status="published", has_minutes=False)
    base.update(kw)
    return Agenda(**base)


class NeverNotify(unittest.TestCase):
    def test_send_controls_are_refused(self):
        self.assertTrue(looks_like_send_control("", "Send", "", ""))
        self.assertTrue(looks_like_send_control("SAVE AND SEND", "", "", ""))
        self.assertTrue(looks_like_send_control("", "", "btnSendAgenda", ""))
        self.assertTrue(looks_like_send_control(
            "", "Go", "", "sendEmailBE(event, 'aspnetForm','AgendaCenter', true, {comingFrom : 'x'})"))
        self.assertTrue(looks_like_send_control("", "", "", "agendaActions(1, 'send', {})"))

    def test_ordinary_controls_pass(self):
        self.assertFalse(looks_like_send_control("SAVE", "", "btnSaveAgenda", "saveAgenda('save', 0); return false;"))
        self.assertFalse(looks_like_send_control("Unpublish", "", "lnkUnpublish", "agendaBulkActions(this, 'unpublish')"))
        self.assertFalse(looks_like_send_control("", "Don't Send", "",
                                                 "sendEmailBE(event, 'aspnetForm', 'AgendaCenter', false, {})"))


class Filters(unittest.TestCase):
    def test_dates_status_description(self):
        f = AgendaFilter.from_dict({"meeting_from": "2024-01-01", "meeting_to": "6/30/2024",
                                    "statuses": ["published"], "description_contains": "regular"})
        self.assertTrue(f.cheap_match(agenda()))
        self.assertFalse(f.cheap_match(agenda(meeting_date=date(2023, 12, 31))))
        self.assertFalse(f.cheap_match(agenda(status="draft", published=False)))
        self.assertFalse(f.cheap_match(agenda(description="Minutes")))

    def test_updated_range_and_missing_date(self):
        f = AgendaFilter.from_dict({"updated_from": "2025-01-01"})
        self.assertFalse(f.cheap_match(agenda()))
        self.assertFalse(f.cheap_match(agenda(updated=None)))
        self.assertTrue(f.cheap_match(agenda(updated=date(2025, 3, 1))))

    def test_categories_case_insensitive_and_exclude(self):
        f = AgendaFilter.from_dict({"categories": ["bikeways committee"], "exclude_categories": ["EDC"]})
        self.assertTrue(f.category_selected(Category(39, "Bikeways Committee", True)))
        self.assertFalse(f.category_selected(Category(55, "EDC", True)))
        self.assertEqual(f.unknown_categories([Category(39, "Bikeways Committee", True)]), [])

    def test_kinds(self):
        f = AgendaFilter.from_dict({"kinds": ["complex"]})
        self.assertTrue(f.needs_details)
        self.assertTrue(f.detail_match(AgendaDetails(False, "01011999.pdf", "", 0, item_count=2)))
        self.assertFalse(f.detail_match(AgendaDetails(True, "a.pdf", "m.docx", 5)))

    def test_bad_input_rejected(self):
        with self.assertRaises(ValueError):
            AgendaFilter.from_dict({"kinds": ["weird"]})
        with self.assertRaises(ValueError):
            AgendaFilter.from_dict({"meeting_from": "2025-01-01", "meeting_to": "2024-01-01"})

    def test_round_trip(self):
        f = AgendaFilter.from_dict({"categories": ["X"], "meeting_from": "2024-01-01", "limit": 5})
        self.assertEqual(AgendaFilter.from_dict(f.to_dict()), f)


class Kinds(unittest.TestCase):
    def test_details_kind(self):
        self.assertEqual(AgendaDetails(True, "a.pdf", "", 0).kind, "simple")
        self.assertEqual(AgendaDetails(True, "a.pdf", "m.pdf", 3).kind, "simple")
        self.assertEqual(AgendaDetails(False, "", "m.pdf", 3).kind, "minutes_only")
        self.assertEqual(AgendaDetails(False, "01011999.pdf", "", 0, item_count=2).kind, "complex")
        self.assertEqual(AgendaDetails(False, "", "", 0).kind, "empty")


class Naming(unittest.TestCase):
    def test_templates_when_no_description(self):
        o = ImportOptions()
        self.assertEqual(name_for("simple", "City Council", "", o), "City Council Regular Meeting Agenda (PDF)")
        self.assertEqual(name_for("minutes_only", "City Council", "", o), "City Council Minutes (PDF)")
        self.assertEqual(name_for("complex", "City Council", "", o), "City Council Regular Meeting Agenda")

    def test_source_description_wins_unless_turned_off(self):
        self.assertEqual(name_for("simple", "C", "Special Meeting", ImportOptions()), "Special Meeting")
        o = ImportOptions(use_source_descriptions=False)
        self.assertEqual(name_for("simple", "C", "Special Meeting", o), "C Regular Meeting Agenda (PDF)")


class Duplicates(unittest.TestCase):
    def m(self, **kw):
        base = dict(category="Bikeways Committee", meeting_date=date(2024, 6, 4), description="x",
                    source_description="", agenda_file="A.pdf", minutes_file="M.docx")
        base.update(kw)
        return Meeting(**base)

    def test_same_files_is_duplicate(self):
        det = AgendaDetails(True, "a.pdf", "m.docx", 1)
        self.assertTrue(is_duplicate(self.m(), agenda(), lambda a: det))

    def test_same_date_different_files_is_not(self):
        det = AgendaDetails(True, "other.pdf", "m.docx", 1)
        self.assertFalse(is_duplicate(self.m(), agenda(), lambda a: det))

    def test_different_date_is_not(self):
        det = AgendaDetails(True, "a.pdf", "m.docx", 1)
        self.assertFalse(is_duplicate(self.m(), agenda(meeting_date=date(2024, 6, 5)), lambda a: det))

    def test_minutes_only_matches_builder_agenda(self):
        det = AgendaDetails(False, "", "m.docx", 1)
        self.assertTrue(is_duplicate(self.m(agenda_file=""), agenda(), lambda a: det))


class Loading(unittest.TestCase):
    def make_workbook(self, folder: Path) -> Path:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "AgendaCenter"
        ws.append(["Category", "Category Description", "Agenda Description", "Meeting Date",
                   "Minutes Available", "Agenda File Name", "Minutes File Name"])
        ws.append(["Board A", None, "Special Meeting", datetime(2024, 1, 8), True, "a1.pdf", "m1.docx"])
        ws.append(["Board A", None, None, datetime(2024, 2, 5), False, None, None])
        ws.append(["Board A", None, None, datetime(2024, 3, 4), True, None, "m3.pdf"])
        ws.append(["Board B", None, None, datetime(2024, 4, 1), False, "missing.pdf", None])
        items = wb.create_sheet("AgendaCenterItems")
        items.append(["Name", "Category", "Meeting Date", "Meeting Time", "Description", "Is Default Item",
                      "File Name 1", "File Name 2"])
        items.append(["Agenda", "Board A", datetime(2024, 2, 5), None, None, False, "i1.pdf", "i2.pdf"])
        p = folder / "wb.xlsx"
        wb.save(p)
        for f in ("a1.pdf", "m1.docx", "m3.pdf", "i1.pdf", "i2.pdf"):
            (folder / f).write_bytes(b"x")
        return p

    def test_synthetic_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            wb = self.make_workbook(folder)
            js = folder / "tasks.json"
            js.write_text(json.dumps({"meetings": [{
                "category": "Board A", "meetingDate": "2024-01-08", "agendaDescription": "Special Meeting",
                "tasks": ["save-to-build-html"], "minutes": None,
                "mediaLinks": {"linkToMedia": "https://video.example/1", "additionalVideoLink": "", "overflow": []}}]}))
            meetings, warnings = load_meetings(ImportOptions(workbook=wb, json_path=js))
            self.assertEqual(warnings, [])
            by_date = {m.meeting_date: m for m in meetings}
            self.assertEqual(by_date[date(2024, 1, 8)].kind, "simple")
            self.assertEqual(by_date[date(2024, 1, 8)].description, "Special Meeting")
            self.assertEqual(by_date[date(2024, 1, 8)].video_link, "https://video.example/1")
            self.assertEqual(by_date[date(2024, 2, 5)].kind, "complex")
            self.assertEqual(by_date[date(2024, 2, 5)].items[0].files, ["i1.pdf", "i2.pdf"])
            self.assertEqual(by_date[date(2024, 3, 4)].kind, "minutes_only")
            self.assertEqual(by_date[date(2024, 3, 4)].description, "Board A Minutes (PDF)")
            self.assertIn("missing file(s): missing.pdf", by_date[date(2024, 4, 1)].problems)

    def test_category_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            wb = self.make_workbook(Path(tmp))
            meetings, _ = load_meetings(ImportOptions(workbook=wb, category_map={"Board A": "Board Alpha"}))
            self.assertIn("Board Alpha", {m.category for m in meetings})

    def test_non_import_workbook_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = self.make_workbook(Path(tmp))
            other = openpyxl.Workbook()
            other.active.title = "Sheet1"
            other.active.append(["EXAMPLES AND FORMATS FOR FIVE YEAR HISTORY"])
            bad = Path(tmp) / "history.xlsx"
            other.save(bad)
            with self.assertRaises(ValueError) as cm:
                load_meetings(ImportOptions(workbook=bad))
            self.assertIn("no 'AgendaCenter' sheet", str(cm.exception))
            self.assertIn(str(good), str(cm.exception))
            with self.assertRaises(ValueError):
                Plan("p", "", [Step("upload_agendas", {"workbook": str(bad)})]).validate()

    def test_find_import_files_in_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "import"
            sub.mkdir()
            good = self.make_workbook(sub)
            (Path(tmp) / "x-post-import-tasks.json").write_text("{}")
            wbs, jsons = find_import_files(Path(tmp))
            self.assertEqual(wbs, [good])
            self.assertEqual([p.name for p in jsons], ["x-post-import-tasks.json"])

    @unittest.skipUnless(REAL_WB.exists(), "real Talbot workbook not on this machine")
    def test_real_talbot_files(self):
        meetings, warnings = load_meetings(ImportOptions(workbook=REAL_WB, json_path=REAL_JSON))
        wb_rows = [m for m in meetings if m.source == "workbook"]
        self.assertEqual(len(wb_rows), 248)
        kinds = {}
        for m in wb_rows:
            kinds[m.kind] = kinds.get(m.kind, 0) + 1
        self.assertEqual(kinds.get("simple"), 208)
        self.assertEqual(kinds.get("complex"), 40)
        ok_simple = [m for m in wb_rows if m.kind == "simple" and not m.problems]
        self.assertEqual(len(ok_simple), 208)
        # JSON "attach-minutes" lands on the complex agenda as a later minutes file.
        later = [m for m in wb_rows if m.attach_minutes_file]
        self.assertEqual(len(later), 27)
        self.assertTrue(all(m.kind == "complex" and m.later_minutes == m.attach_minutes_file for m in later))
        self.assertIn("talbotcountyga-agenda-center-attach-after-import",
                      " ".join(t for m in later for t in m.json_tasks))
        # JSON-only meetings: items make a builder agenda, minutes come later.
        manual = {str(m.meeting_date): m for m in meetings if m.source == "json"}
        self.assertEqual(sorted(manual), ["2026-05-26", "2026-06-23", "2026-08-27", "2026-09-08"])
        self.assertEqual(manual["2026-06-23"].kind, "complex")
        self.assertEqual([i.name for i in manual["2026-06-23"].items], ["Meeting Notes"])
        hearing = manual["2026-08-27"]
        self.assertEqual((hearing.kind, len(hearing.items[0].files), hearing.later_minutes),
                         ("complex", 4, "minutes_public_hearing_2_08_27_26.docx"))
        self.assertEqual(manual["2026-05-26"].kind, "simple")
        self.assertTrue(all(not m.problems for m in manual.values()) or not REAL_ATTACH.exists())

    def test_minutes_only_later_minutes(self):
        m = Meeting(category="C", meeting_date=date(2024, 1, 1), description="d", source_description="",
                    minutes_file="m.pdf")
        self.assertEqual(m.later_minutes, "m.pdf")
        m2 = Meeting(category="C", meeting_date=date(2024, 1, 1), description="d", source_description="",
                     items=[ImportItem("Agenda", ["a.pdf"])], attach_minutes_file="x.pdf")
        self.assertEqual((m2.kind, m2.later_minutes), ("complex", "x.pdf"))
        self.assertIn("x.pdf", m2.all_files())


class Plans(unittest.TestCase):
    def test_round_trip_and_validation(self):
        plan = Plan("t", "https://x", [Step("delete_agendas", {"filter": {"categories": ["A"]}}),
                                       Step("create_categories", {"names": ["B"], "publish": True})])
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "p.json"
            plan.save(p)
            again = Plan.load(p)
        self.assertEqual(again.to_dict(), plan.to_dict())
        with self.assertRaises(ValueError):
            Plan("bad", "", [Step("upload_agendas", {})]).validate()

    @unittest.skipUnless(REAL_WB.exists(), "example plan points at the Talbot test files")
    def test_example_plan_is_valid(self):
        Plan.load(Path(__file__).resolve().parent.parent / "plans/example_cleanup_then_upload.json")


class Items(unittest.TestCase):
    def item(self, **kw):
        base = dict(id=1, category_id=5, category_name="Board", name="Call to Order", status="draft",
                    default=False, meeting_date=date(2026, 3, 1))
        base.update(kw)
        return Item(**base)

    def test_filter(self):
        f = ItemFilter.from_dict({"statuses": ["draft"], "name_contains": "call", "meeting_from": "2026-01-01"})
        self.assertTrue(f.match(self.item()))
        self.assertFalse(f.match(self.item(status="approved")))
        self.assertFalse(f.match(self.item(meeting_date=date(2025, 1, 1))))
        # Default items have no meeting date, so a date filter leaves them out.
        self.assertFalse(f.match(self.item(default=True, meeting_date=None)))
        self.assertTrue(ItemFilter.from_dict({}).match(self.item(default=True, meeting_date=None)))

    def test_validation(self):
        self.assertEqual(ItemFilter.from_dict({}).lists, ["items"])
        for bad in ({"lists": []}, {"lists": ["agendas"]}, {"statuses": ["published"]},
                    {"meeting_from": "2026-02-01", "meeting_to": "2026-01-01"}):
            with self.assertRaises(ValueError):
                ItemFilter.from_dict(bad)

    def test_steps(self):
        plan = Plan(steps=[Step.from_dict({"action": "delete_items", "filter": {"lists": ["default_items"]}}),
                           Step.from_dict({"action": "delete_categories", "categories": ["X"],
                                           "delete_contents_first": True})])
        plan.validate()
        self.assertIn("default items", plan.steps[0].describe())
        self.assertIn("workflows", plan.steps[1].describe())
        self.assertTrue(delete_contents_first({"delete_agendas_first": True}))   # older plan files
        self.assertFalse(delete_contents_first({}))


if __name__ == "__main__":
    unittest.main()
