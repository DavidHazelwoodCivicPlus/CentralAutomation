"""Post-import tasks: loading the migration tool's file, turning it into to-do
steps, and running those steps against a stand-in site (no browser)."""

import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from autocentral.agendacenter.models import Agenda, Category
from autocentral.agendacenter.plan import Plan, Runner, Step
from autocentral.agendacenter.tasks import (CREATE, MEDIA, MINUTES, SAVE, PostTask, counts, find_agenda,
                                            load_tasks, no_agenda_template_pdf, to_meeting, todo_steps)
from autocentral.reporting import Reporter

REAL = Path.home() / "Downloads" / "columbiact-agenda-center-post-import-tasks(1).json"


def meeting(cat, d, desc, tasks, **kw):
    m = {"category": cat, "meetingDate": d, "agendaDescription": desc, "sourceNodeId": "1", "sourceTitle": desc,
         "sourceUrl": "", "tasks": tasks, "minutes": None,
         "mediaLinks": {"linkToMedia": "", "additionalVideoLink": "", "overflow": []},
         "expect": {"itemBased": True, "itemsImported": [], "agendaFileName": "", "minutesFileName": ""}}
    m.update(kw)
    return m


def tasks_json(folder: Path) -> Path:
    data = {"schemaVersion": 1, "generatedBy": "D10-to-Central Migration Tool", "meetings": [
        meeting("Council", "2024-01-02", "Regular Meeting", [MINUTES, SAVE],
                minutes={"fileName": "m1.pdf", "sourceUrl": "https://old/m1.pdf", "localFolder": "x-attach"}),
        meeting("Council", "2024-02-06", "Work Session", [MEDIA, SAVE],
                mediaLinks={"linkToMedia": "https://youtu.be/abc", "additionalVideoLink": "", "overflow": []}),
        meeting("Council", "2024-03-05", "Regular Meeting", [SAVE]),
        meeting("Council", "2024-03-05", "Budget Hearing", [CREATE], manualMeeting={
            "keptInImportInstead": "9", "agendaFile": None, "usesNoAgendaTemplate": False,
            "minutes": [{"fileName": "bh_min.pdf", "sourceUrl": "https://old/bh_min.pdf"}],
            "items": [{"name": "Agenda Packet", "description": "Packet",
                       "files": [{"fileName": "bh_packet.pdf", "sourceUrl": "https://old/bh_packet.pdf"}]}],
            "localFolder": "x-attach"}),
        meeting("Parks", "2024-01-09", "Parks Meeting", [CREATE], manualMeeting={
            "keptInImportInstead": "8", "agendaFile": None, "usesNoAgendaTemplate": True,
            "minutes": [{"fileName": "p_min.pdf", "sourceUrl": ""}], "items": [], "localFolder": "x-attach"}),
    ]}
    p = folder / "x-agenda-center-post-import-tasks.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class Loading(unittest.TestCase):
    def test_json(self):
        with tempfile.TemporaryDirectory() as d:
            tasks, warnings = load_tasks(tasks_json(Path(d)))
        self.assertEqual(warnings, [])
        self.assertEqual(counts(tasks), {CREATE: 2, MINUTES: 1, MEDIA: 1, SAVE: 3})
        self.assertEqual(tasks[0].minutes_file, "m1.pdf")
        self.assertEqual(tasks[1].video, "https://youtu.be/abc")

    def test_csv_without_manual_details(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.csv"
            with open(p, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["Category", "Meeting Date", "Agenda Description (as imported)", "Source Node ID",
                            "Source Title", "Tasks", "Minutes File Name to attach", "Minutes Source URL",
                            "Link to Media", "Additional Video Link", "More Video Links (no field for these)"])
                w.writerow(["Council", "2024-01-02", "Regular Meeting", "1", "x", "attach-minutes; save-to-build-html",
                            "m1.pdf", "https://old/m1.pdf", "", "", ""])
                w.writerow(["Council", "2024-03-05", "Budget Hearing", "2", "x", "create-meeting-manually",
                            "", "", "", "", ""])
            tasks, warnings = load_tasks(p)
        self.assertEqual(tasks[0].tasks, [MINUTES, SAVE])
        self.assertIn("Load the .json", warnings[0])
        self.assertEqual(counts(tasks)[CREATE], 0)

    def test_to_meeting_shapes(self):
        with tempfile.TemporaryDirectory() as d:
            tasks, _ = load_tasks(tasks_json(Path(d)))
        complex_ = to_meeting(tasks[3])
        self.assertEqual(complex_.kind, "complex")
        self.assertEqual(complex_.attach_minutes_file, "bh_min.pdf")
        self.assertEqual(complex_.file_urls["bh_packet.pdf"], "https://old/bh_packet.pdf")
        minutes_only = to_meeting(tasks[4])          # the No Agenda template isn't needed here
        self.assertEqual((minutes_only.kind, minutes_only.agenda_file, minutes_only.minutes_file),
                         ("minutes_only", "", "p_min.pdf"))

    def test_find_agenda(self):
        a = lambda i, d, desc: Agenda(i, 1, "Council", d, None, desc, True, "published", False)
        t = PostTask("Council", date(2024, 3, 5), "Regular Meeting")
        self.assertEqual(find_agenda(t, [a(1, date(2024, 3, 5), "Budget Hearing"),
                                         a(2, date(2024, 3, 5), "Regular Meeting")])[0].id, 2)
        self.assertEqual(find_agenda(t, [a(3, date(2024, 3, 5), "Regular Meeting Agenda")])[0].id, 3)
        self.assertIsNone(find_agenda(t, [a(4, date(2024, 3, 5), "A"), a(5, date(2024, 3, 5), "B")])[0])
        self.assertIsNone(find_agenda(t, [a(6, date(2024, 3, 6), "Regular Meeting")])[0])

    def test_steps_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = tasks_json(Path(d))
            tasks, _ = load_tasks(p)
            steps = todo_steps(p, Path(d), tasks)
            plan = Plan(steps=[Step.from_dict(s) for s in steps])
            plan.validate()
        self.assertEqual([s["action"] for s in steps], ["create_categories"] + ["post_import_tasks"] * 4)
        self.assertEqual([s.get("kinds") for s in steps[1:]], [[CREATE], [MINUTES], [MEDIA], [SAVE]])
        self.assertEqual(steps[0]["names"], ["Council", "Parks"])
        self.assertIn("attach minutes", plan.steps[2].describe())

    def test_template_pdf(self):
        pdf = no_agenda_template_pdf()
        self.assertTrue(pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"No Agenda", pdf)

    @unittest.skipUnless(REAL.exists(), "Columbia tasks file not on this machine")
    def test_real_columbia_file(self):
        tasks, warnings = load_tasks(REAL)
        self.assertEqual(len(tasks), 798)
        self.assertEqual(counts(tasks), {CREATE: 93, MINUTES: 521, MEDIA: 110, SAVE: 697})
        made = [to_meeting(t) for t in tasks if CREATE in t.tasks]
        self.assertTrue(all(m is not None for m in made))


class FakeBrowser:
    class C:
        def check(self):
            pass
    cancel = C()


class FakeSite:
    """Just enough of AgendaCenterSite for the post-import steps."""

    def __init__(self):
        self.b = FakeBrowser()
        self.cats = [Category(1, "Council", True)]
        self.list = {1: [Agenda(10, 1, "Council", date(2024, 1, 2), None, "Regular Meeting", True, "published", False),
                         Agenda(11, 1, "Council", date(2024, 2, 6), None, "Work Session", False, "draft", False),
                         Agenda(12, 1, "Council", date(2024, 3, 5), None, "Regular Meeting", True, "published",
                                False)]}
        self.calls = []

    def categories(self, refresh=False):
        return self.cats

    def find_category(self, name, refresh=False):
        return next((c for c in self.cats if c.name.lower() == name.lower()), None)

    def ensure_view_options(self, cat):
        pass

    def agendas(self, cat):
        return self.list.get(cat.id, [])

    def video_links(self, cat, aid):
        return ("", "")

    def edit_and_save(self, cat, aid, video=None, additional=None):
        self.calls.append(("save", aid, video))
        return "ok"

    def add_minutes(self, cat, aid, path, publish=True):
        self.calls.append(("minutes", aid, path.name))
        return "ok"


class Running(unittest.TestCase):
    def run_steps(self, live):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        p = tasks_json(d)
        (d / "x-attach").mkdir()
        (d / "x-attach" / "m1.pdf").write_bytes(b"%PDF")
        tasks, _ = load_tasks(p)
        steps = [s for s in todo_steps(p, d, tasks) if s["action"] == "post_import_tasks"
                 and s["kinds"] != [CREATE]]
        site = FakeSite()
        rep = Reporter()
        Runner(site, rep, live=live).run(Plan(steps=[Step.from_dict(s) for s in steps]))
        self.tmp.cleanup()
        return site, rep

    def test_dry_run_changes_nothing(self):
        site, rep = self.run_steps(live=False)
        self.assertEqual(site.calls, [])
        got = [(r.action, r.target_id, r.status) for r in rep.results]
        self.assertIn(("add_minutes", "10", "dry-run"), got)
        self.assertIn(("video_links", "11", "dry-run"), got)
        self.assertIn(("save", "11", "skipped"), got)          # its video-link save builds the page
        self.assertIn(("save", "10", "dry-run"), got)
        self.assertIn(("save", "12", "dry-run"), got)

    def test_live(self):
        site, rep = self.run_steps(live=True)
        self.assertIn(("minutes", 10, "m1.pdf"), site.calls)
        self.assertIn(("save", 11, "https://youtu.be/abc"), site.calls)
        self.assertEqual(sum(1 for c in site.calls if c[0] == "save" and c[1] == 11), 1)
        self.assertTrue(all(r.status in ("ok", "skipped") for r in rep.results), rep.results)


if __name__ == "__main__":
    unittest.main()
