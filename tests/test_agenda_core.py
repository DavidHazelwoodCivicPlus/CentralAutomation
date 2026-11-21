"""Offline tests: no browser, no site. Run with:  python -m unittest -v"""

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from autocentral.browser import looks_like_send_control
from autocentral.agendacenter.filters import AgendaFilter, ItemFilter
from autocentral.agendacenter.models import Agenda, AgendaDetails, Category, Item
from autocentral.agendacenter.plan import Plan, Step, delete_contents_first

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


class Plans(unittest.TestCase):
    def test_round_trip_and_validation(self):
        plan = Plan("t", "https://x", [Step("delete_agendas", {"filter": {"categories": ["A"]}}),
                                       Step("create_categories", {"names": ["B"], "publish": True})])
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "p.json"
            plan.save(p)
            again = Plan.load(p)
        self.assertEqual(again.to_dict(), plan.to_dict())
        with self.assertRaises(ValueError):          # uploading moved to the D10 Agenda Center tool
            Step.from_dict({"action": "upload_agendas"})


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
