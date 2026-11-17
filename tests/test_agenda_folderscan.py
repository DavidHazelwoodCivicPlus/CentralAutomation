"""Offline tests for building an upload workbook from a folder of files."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from autocentral.agendacenter import importdata
from autocentral.agendacenter.folderscan import (AGENDA, MINUTES, OTHER, UNKNOWN, ScanOptions, meeting_type, name_date,
                                      name_kind, normalize, pick_six_order, scan, six_digit, text_kind,
                                      write_workbook)
from autocentral.agendacenter.importdata import ImportOptions, find_file, load_meetings


def nd(name):
    return name_date(normalize(name))[0]


class NameDates(unittest.TestCase):
    def test_formats_seen_in_real_exports(self):
        cases = {
            "BOC_07_27_20_Called_Meeting_Minutes": date(2020, 7, 27),
            "march_6_2023_minutes_-_web": date(2023, 3, 6),
            "may_board_meeting_agenda_5-26-2026": date(2026, 5, 26),
            "september_23rd_2024_public_hearing_-_called_meeting_minutes": date(2024, 9, 23),
            "december_2nd_2024_minutes_-_web": date(2024, 12, 2),
            "minutes_public_hearing_2_08_27_26": date(2026, 8, 27),       # not 2/8/27
            "boe_minutes_08192025": date(2025, 8, 19),
            "agenda_82426": date(2026, 8, 24),
            "pz_commission_minutes_april_272026": date(2026, 4, 27),
            "called_meeting_agenda_dec_16_2024": date(2024, 12, 16),
            "PZ Meeting Minutes 02_26_24": date(2024, 2, 26),
            "September 14, 2026 Public Hearing Agenda (1)": date(2026, 9, 14),
            "2024-03-05 Council Agenda": date(2024, 3, 5),
        }
        for name, want in cases.items():
            self.assertEqual(nd(name), want, name)

    def test_no_date_or_partial(self):
        self.assertIsNone(nd("certification_minutes"))
        d, _, partial, _ = name_date(normalize("november_10th_board_of_elections_meeting_minutes"))
        self.assertIsNone(d)
        self.assertEqual(partial, (11, 10))

    def test_six_digit_order_is_chosen_per_folder(self):
        tokens = ["010924", "121919", "111020", "031423"]          # only MMDDYY reads all of them
        self.assertEqual(pick_six_order(tokens), "MMDDYY")
        self.assertEqual(six_digit("121919", "MMDDYY"), date(2019, 12, 19))
        self.assertEqual(pick_six_order(["240109", "231231", "221015"]), "YYMMDD")


class Kinds(unittest.TestCase):
    def test_names(self):
        self.assertEqual(name_kind(normalize("051325_boe_mintues"))[0], MINUTES)          # typo
        self.assertEqual(name_kind(normalize("BOC_01_08_24_Board_Meeting_Agenda"))[0], AGENDA)
        self.assertEqual(name_kind(normalize("BOC_11_07_22_Board_Minutes_Agenda"))[0], UNKNOWN)
        self.assertEqual(name_kind(normalize("2026_notice_of_property_tax_increase"))[0], OTHER)
        self.assertEqual(name_kind(normalize("public_hearing_3_sept_08_2026"))[0], UNKNOWN)

    def test_text(self):
        minutes = ("December 1, 2025 The Board of Commissioners held a Public Hearing on Monday. Chairman Smith "
                   "and Commissioners Jones and Lee were present. Chair Smith called the meeting to order.")
        agenda = ("BOARD OF COMMISSIONERS REGULAR MEETING AGENDA  1. Call to Order  2. Approval of Minutes "
                  "3. Consideration of the budget  4. Adjournment")
        self.assertEqual(text_kind(minutes)[0], MINUTES)
        self.assertEqual(text_kind(agenda)[0], AGENDA)          # "Approval of Minutes" doesn't count
        self.assertEqual(text_kind("Notice of property tax increase")[0], UNKNOWN)

    def test_meeting_type(self):
        self.assertEqual(meeting_type(normalize("BOC_07_20_20_Public_Hearing_Agenda")), "public hearing")
        self.assertEqual(meeting_type(normalize("minutes_public_hearing_2_08_27_26")), "public hearing 2")
        self.assertEqual(meeting_type(normalize("BOC_11_25_20_Workshop_Agenda")), "work session")
        self.assertEqual(meeting_type(normalize("BOC_01_08_24_Board_Meeting_Agenda")), "")


class FolderScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "export"
        files = {
            "Board of Commissioners": ["BOC_01_08_24_Board_Meeting_Agenda.pdf", "january_8_2024_minutes_-_web.docx",
                                       "BOC_07_20_20_Board_Meeting_Agenda.pdf", "BOC_07_20_20_Public_Hearing_Agenda.pdf",
                                       "BOC_07_20_20_Public_Hearing_Minutes.pdf", "BOC_07_20_20_Board_Meeting_Minutes.pdf",
                                       "certification_minutes.pdf", "2024_notice_of_tax_increase_01-08-2024.pdf"],
            "Planning and Zoning": ["010924_pz_minutes.pdf"],
        }
        for cat, names in files.items():
            (self.root / cat).mkdir(parents=True)
            for n in names:
                (self.root / cat / n).write_bytes(b"")
        (self.root / "Board of Commissioners" / "desktop.ini").write_bytes(b"")
        importdata._TREE_CACHE.clear()
        self.res = scan(ScanOptions(root=self.root, read_text=False))

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return {(m.category, m.meeting_date, m.agenda.name if m.agenda else None,
                 m.minutes.name if m.minutes else None) for m in self.res.meetings}

    def test_pairs_and_categories(self):
        rows = self.rows()
        self.assertIn(("Board of Commissioners", date(2024, 1, 8), "BOC_01_08_24_Board_Meeting_Agenda.pdf",
                       "january_8_2024_minutes_-_web.docx"), rows)
        # Two meetings on one day are kept apart by type.
        self.assertIn(("Board of Commissioners", date(2020, 7, 20), "BOC_07_20_20_Public_Hearing_Agenda.pdf",
                       "BOC_07_20_20_Public_Hearing_Minutes.pdf"), rows)
        self.assertIn(("Board of Commissioners", date(2020, 7, 20), "BOC_07_20_20_Board_Meeting_Agenda.pdf",
                       "BOC_07_20_20_Board_Meeting_Minutes.pdf"), rows)
        self.assertIn(("Planning and Zoning", date(2024, 1, 9), None, "010924_pz_minutes.pdf"), rows)
        self.assertEqual(len(rows), 4)
        self.assertNotIn("desktop.ini", [f.name for f in self.res.files])

    def test_review_flags(self):
        jan = next(m for m in self.res.meetings if m.meeting_date == date(2024, 1, 8))
        self.assertTrue(jan.review)                             # the tax notice shares its date
        self.assertEqual([o.name for o in jan.others], ["2024_notice_of_tax_increase_01-08-2024.pdf"])
        hearing = next(m for m in self.res.meetings if m.mtype == "public hearing")
        self.assertFalse(hearing.review)                        # clean type match: no CHECK
        self.assertEqual(hearing.description(), "Board of Commissioners Public Hearing Agenda (PDF)")
        unused = [f.name for f in self.res.files if not f.meeting_date]
        self.assertEqual(unused, ["certification_minutes.pdf"])

    def test_workbook_round_trip(self):
        out = write_workbook(self.res, Path(self.tmp.name) / "scan.xlsx", text_reads=False)
        meetings, warnings = load_meetings(ImportOptions(workbook=out))
        self.assertEqual(warnings, [])
        self.assertEqual(len(meetings), 4)
        held = [m for m in meetings if m.problems]
        self.assertEqual(len(held), 1)                          # the CHECK row
        self.assertIn("Review column", held[0].problems[0])
        ok = [m for m in meetings if not m.problems]
        self.assertTrue(all(m.agenda_file or m.minutes_file for m in ok))   # files found in subfolders

    def test_find_file_looks_in_subfolders_but_not_ambiguously(self):
        # Ordinary workbooks keep the tested rule: top level of the files folder only.
        self.assertIsNone(find_file("010924_pz_minutes.pdf", [self.root]))
        self.assertIsNotNone(find_file("010924_pz_minutes.pdf", [self.root], deep=True))
        (self.root / "Planning and Zoning" / "copy").mkdir()
        (self.root / "Planning and Zoning" / "copy" / "BOC_01_08_24_Board_Meeting_Agenda.pdf").write_bytes(b"")
        importdata._TREE_CACHE.clear()
        self.assertIsNone(find_file("BOC_01_08_24_Board_Meeting_Agenda.pdf", [self.root], deep=True))

    def test_upload_step_finds_scan_files_without_a_files_folder(self):
        out = write_workbook(self.res, Path(self.tmp.name) / "scan.xlsx", text_reads=False)
        files_dir, deep = importdata.files_search(ImportOptions(workbook=out))
        self.assertEqual((files_dir, deep), (self.root, True))
        # An ordinary import workbook: its own folder, top level only (as before).
        plain = Path(self.tmp.name) / "plain.xlsx"
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.title = "AgendaCenter"
        wb.save(plain)
        self.assertEqual(importdata.files_search(ImportOptions(workbook=plain)), (plain.parent, False))


if __name__ == "__main__":
    unittest.main()
