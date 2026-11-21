"""Agenda Center actions in the window: publish, unpublish and delete the
ticked rows (agendas, or whole categories), and undo a live run.

Every job runs as a dry run first, in a run window that lists what would
happen. "Run for real" only unlocks after that dry run, and asks for the
site's address to be typed. Live runs keep results.csv and undo.json in
runs/<site>/<time>_live; "Undo a live run…" turns an undo.json into the
steps that reverse it.

(D10 migrations are done with the separate D10 Agenda Center migration tool.)
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Optional

from .agendacenter.jobs import AgendaJobs, Selection
from .cli import ROOT
from .reporting import ActionResult

RUNS = ROOT / "runs"


def confirm_site(parent, host: str, what: str) -> bool:
    """The person types the site's address to start a live run."""
    typed = simpledialog.askstring(
        "Run for real", f"{what}\n\nThis changes {host} for real. Deletes can't be undone.\n\n"
                        f"Type the site address to continue:\n{host}", parent=parent)
    if typed is None:
        return False
    if typed.strip().lower() != host.lower():
        messagebox.showwarning("Run for real", "That isn't the site address; nothing was changed.", parent=parent)
        return False
    return True


class RunWindow:
    """One job: dry run, then (optionally) the same job for real."""

    COLS = [("status", "Status", 80), ("action", "Action", 90), ("type", "Type", 80), ("category", "Category", 180),
            ("date", "Meeting date", 95), ("desc", "Name / file", 300), ("id", "ID", 70), ("detail", "Detail", 420)]

    def __init__(self, app, title: str, what: str, job: Callable[[AgendaJobs], None],
                 factory: Optional[Callable] = None, module: str = "agenda") -> None:
        """job(jobs) does the work; factory(browser, reporter, live) makes the
        jobs object (default: Agenda Center's). module: the tab read again
        after a live run."""
        self.app, self.what, self.job = app, what, job
        self.factory, self.module = factory, module
        self.top = tk.Toplevel(app.root)
        self.top.title(title)
        self.top.geometry("1300x650")
        pad = {"padx": 6, "pady": 4}
        ttk.Label(self.top, text=what, wraplength=1200, justify="left").pack(fill="x", **pad)
        self.state = tk.StringVar(value="Dry run starting…")
        ttk.Label(self.top, textvariable=self.state, foreground="#555").pack(fill="x", **pad)
        mid = ttk.Frame(self.top)
        mid.pack(fill="both", expand=True, **pad)
        self.tree = ttk.Treeview(mid, columns=[k for k, _, _ in self.COLS], show="headings")
        for k, label, w in self.COLS:
            self.tree.heading(k, text=label)
            self.tree.column(k, width=w, stretch=k in ("desc", "detail"))
        ys = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        for st, color in (("error", "#b00020"), ("skipped", "#a15c00"), ("ok", "#1b7f3b")):
            self.tree.tag_configure(st, foreground=color)
        bar = ttk.Frame(self.top)
        bar.pack(fill="x", **pad)
        self.btn_live = ttk.Button(bar, text="Run for real…", command=self.run_live, state="disabled")
        self.btn_live.pack(side="left")
        ttk.Button(bar, text="Open run folder", command=self.open_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=self.top.destroy).pack(side="right")
        self.run_dir: Optional[Path] = None
        self.live = False
        self.counts: dict[str, int] = {}
        self.start(live=False)

    def start(self, live: bool) -> None:
        self.live = live
        self.tree.delete(*self.tree.get_children())
        self.counts = {}
        self.btn_live.configure(state="disabled")
        self.state.set(("Running for real…" if live else "Dry run (nothing changes)…") + " See the Log for progress.")
        self.run_dir = self.app.run_job(self, live)

    def run_live(self) -> None:
        if confirm_site(self.top, self.app.worker.browser.host, self.what):
            self.start(live=True)

    def add_result(self, r: ActionResult) -> None:
        self.counts[r.status] = self.counts.get(r.status, 0) + 1
        self.tree.insert("", "end", tags=(r.status,), values=[r.status, r.action, r.target_type, r.category,
                                                              r.meeting_date, r.description, r.target_id, r.detail])
        self.tree.yview_moveto(1.0)

    def finished(self, ok: bool) -> None:
        summary = ", ".join(f"{v} {k}" for k, v in sorted(self.counts.items())) or "nothing to do"
        if self.live:
            self.state.set(f"Done: {summary}. Results and undo list: {self.run_dir}")
        else:
            self.state.set(f"Dry run done: {summary}. Nothing was changed. "
                           + ("Check the list, then 'Run for real…'." if ok and self.counts else ""))
            if ok and self.counts.get("dry-run"):
                self.btn_live.configure(state="normal")

    def open_folder(self) -> None:
        if self.run_dir and self.run_dir.exists():
            os.startfile(self.run_dir)


class AgendaActions:
    """The action bar inside the Agenda Center tab."""

    def __init__(self, app, tab) -> None:
        self.app, self.tab = app, tab
        box = ttk.LabelFrame(tab.frame, text="Agenda Center actions")
        box.pack(fill="x", padx=4, pady=3, after=tab.head)
        self.buttons = [
            ttk.Button(box, text="Publish ticked…", command=lambda: self.ticked("publish")),
            ttk.Button(box, text="Unpublish ticked…", command=lambda: self.ticked("unpublish")),
            ttk.Button(box, text="Delete ticked…", command=lambda: self.ticked("delete")),
            ttk.Button(box, text="Undo a live run…", command=self.undo),
        ]
        for b in self.buttons:
            b.pack(side="left", padx=4, pady=3)
        more = [
            ttk.Button(box, text="Delete everything in this module…", command=self.everything),
            ttk.Button(box, text="Create categories…", command=self.create),
        ]
        for b in more:
            b.pack(side="left", padx=4, pady=3)
        self.buttons += more
        ttk.Label(box, text="Every action runs as a dry run first. A ticked category means the whole category "
                            "(for Delete: with all its agendas, items, folders and workflows).",
                  foreground="#777", wraplength=420, justify="left").pack(side="left", padx=10)

    def everything(self) -> None:
        """Every category with everything in it (the same delete as ticking them all)."""
        if self.app.worker.browser is None:
            messagebox.showinfo("Agenda Center", "Log in first (Open Chrome and log in).")
            return

        def job(jobs):
            cats = jobs.site.categories(refresh=True)
            jobs.r.info(f"Agenda Center: {len(cats)} categories on the site.")
            jobs.delete(Selection(categories={c.id: c.name for c in cats}))
        RunWindow(self.app, "Agenda Center: delete everything",
                  "Delete EVERYTHING in Agenda Center: every category with all its agendas, items, folders and "
                  "workflows.", job)

    def create(self) -> None:
        """Create categories from a list of names (the extension's Bulk Create)."""
        from .gui_actions import ask_names
        if self.app.worker.browser is None:
            messagebox.showinfo("Agenda Center", "Log in first (Open Chrome and log in).")
            return
        got = ask_names(self.app.root, "Agenda Center")
        if not got:
            return
        names, publish = got
        from .agendacenter.plan import Plan, Step
        plan = Plan(name="Create categories", steps=[Step("create_categories", {"names": names, "publish": publish})])
        RunWindow(self.app, "Agenda Center: create categories",
                  f"Create {len(names)} categor{'y' if len(names) == 1 else 'ies'}"
                  + (" (published)" if publish else " (unpublished)") + ": " + ", ".join(names[:8])
                  + (" …" if len(names) > 8 else "") + ".", lambda jobs: jobs.runner.run(plan))

    def ticked(self, action: str) -> None:
        if self.app.worker.browser is None:
            messagebox.showinfo("Agenda Center", "Log in first (Open Chrome and log in).")
            return
        sel = Selection.from_rows(self.tab.ticked_rows())
        if not sel.categories and not sel.agendas:
            messagebox.showinfo("Agenda Center", "Tick the agendas or categories first.")
            return
        names = ", ".join(list(sel.categories.values())[:5]) + (" …" if len(sel.categories) > 5 else "")
        what = f"{action.capitalize()} {sel.describe()}" + (f" (categories: {names})" if names else "") + "."
        if action == "delete" and sel.categories:
            what += " Each category is deleted with everything in it."
        if action == "publish":
            what += (" Each ticked category is published with all its draft agendas." if sel.categories else "")                 + " Notify Me is answered Don't Send."
        if action == "delete":
            job = lambda jobs: jobs.delete(sel)
        else:
            job = lambda jobs, pub=(action == "publish"): jobs.set_published(sel, pub)
        RunWindow(self.app, f"Agenda Center: {action} ticked", what, job)

    def undo(self) -> None:
        """Reverse a live run: its undo.json (in runs/<site>/<time>_live) turned
        into the steps that put things back, run as a dry run first."""
        from .agendacenter.plan import undo_plan
        if self.app.worker.browser is None:
            messagebox.showinfo("Agenda Center", "Log in first (Open Chrome and log in).")
            return
        p = filedialog.askopenfilename(initialdir=str(RUNS), title="undo.json from a live run",
                                       filetypes=[("Undo file", "undo.json")])
        if not p:
            return
        plan = undo_plan(Path(p))
        if not plan.steps:
            messagebox.showinfo("Undo", "That run has nothing to undo.")
            return
        what = "Undo " + Path(p).parent.name + ": " + "; ".join(s.describe() for s in plan.steps) + "."
        RunWindow(self.app, "Agenda Center: undo a live run", what, lambda jobs: jobs.runner.run(plan))
