"""The report a D10 migration ends with: one HTML page in the work folder
(<site>-migration-report.html), plus CSVs of anything that went wrong.

It says what was done and how long each step took, what couldn't be
downloaded, which rows were left out of the batches and why, what the
Content Import reported, what the to-do list did, and the accuracy check:
every expected meeting against the site.
"""

from __future__ import annotations

import csv
import html
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional


def _fmt(sec: float) -> str:
    sec = int(round(sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02}m {s:02}s" if h else f"{m}m {s:02}s"


def _csv_rows(path: Path) -> list[list[str]]:
    if not path or not Path(path).is_file():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


def _table(rows: list[list], head: list[str], limit: int = 500) -> str:
    if not rows:
        return "<p class=none>None.</p>"
    out = ["<table><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in head) + "</tr>"]
    for r in rows[:limit]:
        out.append("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>")
    out.append("</table>")
    if len(rows) > limit:
        out.append(f"<p class=none>… and {len(rows) - limit} more (see the CSV).</p>")
    return "".join(out)


def write_report(st, site_steps: list[tuple[str, float, str]], results: list, verification=None,
                 site_host: str = "", live: bool = True, out: Optional[Path] = None) -> Path:
    """st: MigrationState. site_steps: [(step, seconds, note)]. results: the
    Reporter's ActionResults. verification: verify.Verification or None."""
    work = Path(st.work)
    out = out or work / f"{st.site}-migration-report.html"
    prep = [("Build the import (migration tool)", st.seconds.get("build"), f"{len(st.categories)} categories"),
            ("Download the files", st.seconds.get("download"),
             ", ".join(f"{v} {k}" for k, v in (st.file_counts or {}).items())),
            ("Make the batches", st.seconds.get("batches"), f"{len(st.zips())} zip(s)")]
    steps = [(n, s, note) for n, s, note in prep if s is not None] + list(site_steps)
    total = sum(s or 0 for _, s, _ in steps)

    by = Counter((r.action, r.status) for r in results)
    actions = sorted({a for a, _ in by})
    statuses = ["ok", "skipped", "dry-run", "error"]
    counts = [[a] + [by.get((a, s), 0) for s in statuses] for a in actions]
    errors = [[r.action, r.category, r.meeting_date, r.description, r.target_id, r.detail]
              for r in results if r.status == "error"]
    if errors:
        with open(work / f"{st.site}-migration-errors.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Action", "Category", "Meeting Date", "Description", "ID", "Detail"])
            w.writerows(errors)

    file_log = work / f"{st.site}-agenda-center-file-log.csv"
    file_rows = _csv_rows(file_log)[1:]
    left = _csv_rows(Path(st.batches_dir) / "left-out.csv")[1:] if st.batches_dir else []

    acc = ""
    if verification is not None:
        rows = [[c, f"{ok} of {tot}", "yes" if ok == tot else "NO"] for c, (ok, tot) in verification.checks.items()]
        probs = [[p.check, p.category, p.meeting_date, p.description, p.agenda_id, p.detail]
                 for p in verification.problems] + \
                [["extra on site", a.category_name, a.meeting_date, a.description, a.id, ""] for a in verification.extra]
        verification.write_csv(work / f"{st.site}-accuracy-problems.csv")
        verdict = ("<p class=ok>100% accurate: every expected meeting is on the site, as expected.</p>"
                   if verification.perfect else
                   f"<p class=bad>{len(probs)} problem(s), listed below and in {st.site}-accuracy-problems.csv.</p>")
        acc = (f"<h2>Accuracy check</h2><p>{verification.found} of {verification.expected} expected meetings found on "
               f"the site; {len(verification.extra)} agenda(s) on the site nobody expected.</p>{verdict}"
               + _table(rows, ["Check", "Passed", "All?"]) + "<h3>Problems</h3>"
               + _table(probs, ["Check", "Category", "Meeting Date", "Description", "Agenda ID", "Detail"]))

    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(st.site)} migration report</title>
<style>body{{font:14px/1.45 Segoe UI,Arial,sans-serif;margin:24px;max-width:1200px;color:#222}}
table{{border-collapse:collapse;margin:6px 0 18px}}td,th{{border:1px solid #ccc;padding:3px 8px;text-align:left;vertical-align:top}}
th{{background:#f0f0f0}}.ok{{color:#176c2f;font-weight:600}}.bad{{color:#b3261e;font-weight:600}}.none{{color:#777}}
h2{{margin-top:28px;border-bottom:1px solid #ddd}}</style></head><body>
<h1>{html.escape(st.site)}: D10 Agenda Center migration</h1>
<p>{'Live run' if live else 'Dry run (nothing on the site was changed)'} on <b>{html.escape(site_host)}</b>,
report written {datetime.now():%Y-%m-%d %H:%M}. Old site's time zone: {html.escape(st.zone or '?')}.
Work folder: {html.escape(str(work))}</p>
<h2>Steps and times</h2>{_table([[n, _fmt(s), note] for n, s, note in steps] + [["Total", _fmt(total), ""]], ["Step", "Time", "Notes"])}
{acc}
<h2>What the site part did</h2>{_table(counts, ["Action"] + statuses)}
<h3>Errors</h3>{_table(errors, ["Action", "Category", "Meeting Date", "Description", "ID", "Detail"])}
<h2>Files that couldn't be downloaded</h2>{_table(file_rows, ["Result", "File", "Reason", "HTTP", "Destination", "Meeting", "Source URL"])}
<h2>Rows left out of the import</h2>{_table(left, ["Sheet", "Row", "Category", "Description", "Why"])}
</body></html>"""
    out.write_text(page, encoding="utf-8")
    return out
