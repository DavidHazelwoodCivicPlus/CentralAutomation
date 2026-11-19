"""Reading category names from a .csv/.xlsx for the to-do list."""

import tempfile
import unittest
from pathlib import Path

import openpyxl

from autocentral.agendacenter.plan import Step
from autocentral.agendacenter.tasks import load_category_names


class CategoryFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def csv(self, text: str) -> Path:
        p = self.dir / "cats.csv"
        p.write_text(text, encoding="utf-8-sig")       # the migration tool writes a BOM
        return p

    def test_migration_tool_csv(self):
        p = self.csv('Name\n"Airport Board"\n"Mayor & Commissioners"\n"Parks & Recreation Board"\n')
        names, warnings = load_category_names(p)
        self.assertEqual(names, ["Airport Board", "Mayor & Commissioners", "Parks & Recreation Board"])
        self.assertEqual(warnings, [])

    def test_name_column_found_anywhere_and_blanks_and_repeats_dropped(self):
        p = self.csv("Description,Category Name\nx, Board  of Ed \ny,\nz,board of ed\n,\n")
        names, warnings = load_category_names(p)
        self.assertEqual(names, ["Board of Ed"])
        self.assertTrue(any("more than once" in w for w in warnings))

    def test_no_header_uses_first_column(self):
        names, warnings = load_category_names(self.csv("Airport Board\nZoning\n"))
        self.assertEqual(names, ["Airport Board", "Zoning"])
        self.assertTrue(warnings)

    def test_xlsx(self):
        p = self.dir / "cats.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        for row in (["Category", "Notes"], ["Airport Board", "a"], [None, None], ["Zoning", ""]):
            ws.append(row)
        wb.save(p)
        names, _ = load_category_names(p)
        self.assertEqual(names, ["Airport Board", "Zoning"])

    def test_other_file_types_refused(self):
        p = self.dir / "cats.json"
        p.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_category_names(p)

    def test_makes_a_valid_step(self):
        names, _ = load_category_names(self.csv("Name\nAirport Board\n"))
        step = Step.from_dict({"action": "create_categories", "names": names, "publish": True})
        self.assertEqual(step.params["names"], ["Airport Board"])

    @unittest.skipUnless((Path.home() / "Downloads" / "cityofhiawatha-agenda-center-categories.csv").is_file(),
                         "real file not present")
    def test_real_hiawatha_file(self):
        names, warnings = load_category_names(Path.home() / "Downloads" / "cityofhiawatha-agenda-center-categories.csv")
        self.assertEqual(len(names), 4)
        self.assertIn("Mayor & Commissioners", names)
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
