"""Agenda Center actions in the window: the to-do list (post-import tasks
and saved lists), compare with an import file, upload what's missing, and
delete / publish / unpublish ticked rows.

Every job runs as a dry run first, in a run window that lists what would
happen. "Run for real" only unlocks after that dry run, and asks for the
site's address to be typed. Live runs keep results.csv and undo.json in
runs/<site>/<time>_live.
"""

from __future__ import annotations

import os
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Optional

from .agendacenter.compare import (MISSING, MISSING_MINUTES, NO_CATEGORY, ON_SITE, ONLY_ON_SITE, Comparison,
                                   compare, export_comparison, site_agendas)
from .agendacenter.d10 import ZONES, find_meetings_json
from .agendacenter.importdata import ImportOptions, load_meetings
from .agendacenter.jobs import AgendaJobs, Selection
from .cli import ROOT
from .reporting import ActionResult

RUNS = ROOT / "runs"
OUTCOMES = [(MISSING, "Missing"), (MISSING_MINUTES, "Missing minutes"), (NO_CATEGORY, "No category on site"),
            (ON_SITE, "On site"), (ONLY_ON_SITE, "Only on site")]


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

    def __init__(self, app, title: str, what: str, job: Callable[[AgendaJobs], None]) -> None:
        self.app, self.what, self.job = app, what, job
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


class CompareWindow:
    """Import workbook (+ D10 export) against the Agenda Center as last read."""

    TREE_COLS = [("category", "Category", 170), ("date", "Meeting date", 90), ("desc", "Description", 280),
                 ("kind", "Kind", 70), ("files", "Files", 330), ("site", "Site ID", 65), ("detail", "Detail", 260),
                 ("notes", "Notes", 300)]

    def __init__(self, app) -> None:
        self.app = app
        self.cmp: Optional[Comparison] = None
        self.meetings = []
        self.opts: Optional[ImportOptions] = None
        s = app.settings.get("agenda_compare", {})
        self.top = tk.Toplevel(app.root)
        self.top.title("Agenda Center: compare with an import file")
        self.top.geometry("1400x800")
        pad = {"padx": 6, "pady": 3}

        src = ttk.LabelFrame(self.top, text="What should be on the site")
        src.pack(fill="x", **pad)
        self.workbook = tk.StringVar(value=s.get("workbook", ""))
        self.d10 = tk.StringVar(value=s.get("d10", ""))
        self.zone = tk.StringVar(value=s.get("zone", "Eastern"))
        self.files = tk.StringVar(value=s.get("files", ""))
        self._path_row(src, 0, "Import workbook (Master_Content_Import.xlsx)", self.workbook,
                       lambda: self._browse_file(self.workbook, [("Excel", "*.xlsx")], after=self._found_workbook))
        self._path_row(src, 1, "D10 meetings export (www-meetings.json, optional)", self.d10,
                       lambda: self._browse_file(self.d10, [("JSON", "*.json")]))
        ttk.Label(src, text="Old site's time zone").grid(row=2, column=0, sticky="e", padx=4)
        ttk.Combobox(src, textvariable=self.zone, values=list(ZONES), width=12, state="readonly").grid(
            row=2, column=1, sticky="w")
        ttk.Label(src, text="The D10 export fills in meeting dates the workbook lacks, and the minutes complex "
                            "agendas lose in the workbook.", foreground="#777").grid(row=2, column=1, sticky="w",
                                                                                      padx=(120, 0))
        self.reread = tk.BooleanVar(value=True)
        bar = ttk.Frame(src)
        bar.grid(row=3, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Checkbutton(bar, text="Read the Agenda Center again first", variable=self.reread).pack(side="left")
        self.btn_compare = ttk.Button(bar, text="Compare", command=self.run_compare)
        self.btn_compare.pack(side="left", padx=8)
        self.summary = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.summary).pack(side="left", padx=8)

        self.nb = ttk.Notebook(self.top)
        self.nb.pack(fill="both", expand=True, **pad)
        self.trees: dict[str, ttk.Treeview] = {}
        for key, label in OUTCOMES:
            f = ttk.Frame(self.nb)
            t = ttk.Treeview(f, columns=[k for k, _, _ in self.TREE_COLS], show="headings")
            for k, lab, w in self.TREE_COLS:
                t.heading(k, text=lab)
                t.column(k, width=w, stretch=k in ("files", "notes"))
            ys = ttk.Scrollbar(f, orient="vertical", command=t.yview)
            t.configure(yscrollcommand=ys.set)
            t.pack(side="left", fill="both", expand=True)
            ys.pack(side="right", fill="y")
            self.nb.add(f, text=label)
            self.trees[key] = t

        up = ttk.LabelFrame(self.top, text="Upload what's missing")
        up.pack(fill="x", **pad)
        self._path_row(up, 0, "Files folder (agenda / minutes / item files)", self.files,
                       lambda: self._browse_dir(self.files))
        opts = ttk.Frame(up)
        opts.grid(row=1, column=0, columnspan=3, sticky="w", pady=3)
        self.download = tk.BooleanVar(value=bool(s.get("download", False)))
        self.publish = tk.BooleanVar(value=bool(s.get("publish", True)))
        self.create_cats = tk.BooleanVar(value=False)
        self.add_minutes = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Publish them (Notify Me is always answered Don't Send)",
                        variable=self.publish).pack(side="left")
        ttk.Checkbutton(opts, text="Add minutes to agendas that are there without them",
                        variable=self.add_minutes).pack(side="left", padx=10)
        ttk.Checkbutton(opts, text="Create missing categories", variable=self.create_cats).pack(side="left")
        ttk.Checkbutton(opts, text="Download files not in the folder from the old site",
                        variable=self.download).pack(side="left", padx=10)
        bar2 = ttk.Frame(up)
        bar2.grid(row=2, column=0, columnspan=3, sticky="w", pady=3)
        self.btn_upload = ttk.Button(bar2, text="Upload missing (dry run first)…", command=self.upload,
                                     state="disabled")
        self.btn_upload.pack(side="left")
        self.btn_export = ttk.Button(bar2, text="Export comparison…", command=self.export, state="disabled")
        self.btn_export.pack(side="left", padx=6)

    # ------------------------------------------------------------- inputs
    def _path_row(self, parent, row: int, label: str, var: tk.StringVar, browse) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(parent, textvariable=var, width=110).grid(row=row, column=1, sticky="w")
        ttk.Button(parent, text="Browse…", command=browse).grid(row=row, column=2, padx=4)

    def _browse_file(self, var, types, after=None) -> None:
        p = filedialog.askopenfilename(parent=self.top, filetypes=types + [("All files", "*.*")],
                                       initialdir=str(Path(var.get()).parent) if var.get() else None)
        if p:
            var.set(p)
            if after:
                after()

    def _browse_dir(self, var) -> None:
        p = filedialog.askdirectory(parent=self.top, initialdir=var.get() or None)
        if p:
            var.set(p)

    def _found_workbook(self) -> None:
        wb = Path(self.workbook.get())
        if not self.files.get():
            self.files.set(str(wb.parent))
        if not self.d10.get():
            hit = find_meetings_json(wb)
            if hit:
                self.d10.set(str(hit))

    def _save_settings(self) -> None:
        self.app.settings["agenda_compare"] = {
            "workbook": self.workbook.get(), "d10": self.d10.get(), "zone": self.zone.get(),
            "files": self.files.get(), "download": self.download.get(), "publish": self.publish.get()}
        self.app.save()

    # ------------------------------------------------------------ compare
    def run_compare(self) -> None:
        if not Path(self.workbook.get()).is_file():
            messagebox.showinfo("Compare", "Pick the import workbook first.", parent=self.top)
            return
        self._save_settings()
        if self.reread.get():
            if self.app.worker.browser is None:
                messagebox.showinfo("Compare", "Log in first (Open Chrome and log in), or untick "
                                               "'Read the Agenda Center again first'.", parent=self.top)
                return
            self.summary.set("Reading the Agenda Center…")
            self.app.read(["agenda"], then=self._compare_now)
        else:
            self._compare_now()

    def _compare_now(self) -> None:
        scan = self.app.tabs["agenda"].scan
        if scan is None or not scan.present:
            self.summary.set("")
            messagebox.showinfo("Compare", "The Agenda Center hasn't been read on this site yet.", parent=self.top)
            return
        self.opts = ImportOptions(workbook=Path(self.workbook.get()),
                                  d10_meetings=Path(self.d10.get()) if self.d10.get().strip() else None,
                                  d10_zone=self.zone.get(),
                                  files_dir=Path(self.files.get()) if self.files.get().strip() else None)
        try:
            self.meetings, warnings = load_meetings(self.opts)
        except Exception as e:
            messagebox.showerror("Compare", f"Couldn't read the import file:\n{e}", parent=self.top)
            return
        for w in warnings:
            self.app.log(f"Import file: {w}", "warn")
        site, cats = site_agendas(scan.rows)
        self.cmp = compare(self.meetings, site, list(cats.values()))
        for key, tree in self.trees.items():
            tree.delete(*tree.get_children())
            for i, r in enumerate(self.cmp.of(key)):
                m = r.meeting
                files = ""
                if m:
                    files = " | ".join(x for x in (m.agenda_file, (m.minutes_file or m.attach_minutes_file) and
                                                   f"minutes: {m.minutes_file or m.attach_minutes_file}",
                                                   *(f"{it.name}: {', '.join(it.files)}" for it in m.items)) if x)
                tree.insert("", "end", values=[r.category, r.meeting_date or "", r.description,
                                                m.kind if m else "", files, r.site.id if r.site else "", r.detail,
                                                "; ".join(m.notes + m.problems) if m else ""])
        c = self.cmp.counts()
        for i, (key, label) in enumerate(OUTCOMES):
            self.nb.tab(i, text=f"{label} ({c[key]})")
        self.summary.set(f"{len(self.meetings)} meetings in the import file (read {scan.read_at}): "
                         f"{c[MISSING]} missing, {c[MISSING_MINUTES]} missing minutes, "
                         f"{c[NO_CATEGORY]} in categories not on the site, {c[ON_SITE]} on site; "
                         f"{c[ONLY_ON_SITE]} site agendas aren't in the file.")
        self.btn_export.configure(state="normal")
        todo = c[MISSING] + c[MISSING_MINUTES] + c[NO_CATEGORY]
        self.btn_upload.configure(state="normal" if todo else "disabled")
        self.app.log(f"Compared with {Path(self.workbook.get()).name}: {self.summary.get()}")

    def export(self) -> None:
        if not self.cmp:
            return
        host = self.app.host().split(".")[0] or "site"
        default = ROOT / "inventories" / f"{host}_agenda_compare_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        default.parent.mkdir(parents=True, exist_ok=True)
        p = filedialog.asksaveasfilename(parent=self.top, initialdir=str(default.parent), initialfile=default.name,
                                         defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if p:
            sources = [self.workbook.get()] + ([self.d10.get()] if self.d10.get() else [])
            export_comparison(Path(p), self.cmp, self.app.site.get(), sources)
            self.app.log(f"Saved {p}")

    # ------------------------------------------------------------- upload
    def upload(self) -> None:
        if not self.cmp or self.app.worker.browser is None:
            messagebox.showinfo("Upload", "Log in and compare first.", parent=self.top)
            return
        self._save_settings()
        rows = [r for r in self.cmp.rows if r.outcome in (MISSING, MISSING_MINUTES) or
                (r.outcome == NO_CATEGORY and self.create_cats.get())]
        if not self.add_minutes.get():
            rows = [r for r in rows if r.outcome != MISSING_MINUTES]
        if not rows:
            messagebox.showinfo("Upload", "Nothing to upload with these options.", parent=self.top)
            return
        self.opts.files_dir = Path(self.files.get()) if self.files.get().strip() else None
        meetings, opts = self.meetings, self.opts
        publish, create, download, minutes = (self.publish.get(), self.create_cats.get(), self.download.get(),
                                              self.add_minutes.get())
        n_new = sum(1 for r in rows if r.outcome != MISSING_MINUTES)
        n_min = len(rows) - n_new
        what = (f"Upload {n_new} missing agenda(s)" + (f" and add minutes to {n_min} agenda(s)" if n_min else "")
                + (", published (Notify Me: Don't Send)" if publish else ", as drafts")
                + (", downloading files that aren't in the folder from the old site" if download else "") + ".")
        RunWindow(self.app, "Agenda Center: upload missing", what,
                  lambda jobs: jobs.upload_missing(rows, meetings, opts, publish, create, download, minutes))


class AgendaActions:
    """The action bar inside the Agenda Center tab."""

    def __init__(self, app, tab) -> None:
        self.app, self.tab = app, tab
        box = ttk.LabelFrame(tab.frame, text="Agenda Center actions")
        box.pack(fill="x", padx=4, pady=3, after=tab.head)
        self.buttons = [
            ttk.Button(box, text="To-do list…", command=lambda: ToDoWindow.show(app)),
            ttk.Button(box, text="Compare with import file…", command=lambda: CompareWindow(app)),
            ttk.Button(box, text="Delete ticked…", command=lambda: self.ticked("delete")),
            ttk.Button(box, text="Unpublish ticked…", command=lambda: self.ticked("unpublish")),
            ttk.Button(box, text="Publish ticked…", command=lambda: self.ticked("publish")),
        ]
        for b in self.buttons:
            b.pack(side="left", padx=4, pady=3)
        ttk.Label(box, text="Every action runs as a dry run first. A ticked category means the whole category "
                            "(for Delete: with all its agendas, items, folders and workflows).",
                  foreground="#777").pack(side="left", padx=10)

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
            what += " Notify Me is answered Don't Send."
        if action == "delete":
            job = lambda jobs: jobs.delete(sel)
        else:
            job = lambda jobs, pub=(action == "publish"): jobs.set_published(sel, pub)
        RunWindow(self.app, f"Agenda Center: {action} ticked", what, job)


# ================================================================ to-do list
TODO_FILE = ROOT / "plans" / "_todo.json"


class ToDoWindow:
    """The to-do list: steps that run top to bottom, as one dry run and then
    for real. Load a post-import tasks file to fill it in, or load a saved
    list. The list is kept between sessions (plans/_todo.json)."""

    _open: Optional["ToDoWindow"] = None

    @classmethod
    def show(cls, app) -> "ToDoWindow":
        if cls._open is not None:
            try:
                cls._open.top.deiconify()
                cls._open.top.lift()
                return cls._open
            except tk.TclError:
                cls._open = None
        cls._open = cls(app)
        return cls._open

    def __init__(self, app) -> None:
        from .agendacenter.plan import Plan
        self.app = app
        self.steps = []
        if TODO_FILE.exists():
            try:
                self.steps = list(Plan.load(TODO_FILE).steps)
            except Exception as e:
                app.log(f"Couldn't read the saved to-do list: {e}", "warn")
        self.top = tk.Toplevel(app.root)
        self.top.title("Agenda Center: to-do list")
        self.top.geometry("1000x520")
        pad = {"padx": 6, "pady": 4}
        ttk.Label(self.top, text="Steps run top to bottom. 'Dry run the list' shows what every step would do; "
                                 "the run window then offers 'Run for real…'.", foreground="#555").pack(fill="x", **pad)
        mid = ttk.Frame(self.top)
        mid.pack(fill="both", expand=True, **pad)
        self.listbox = tk.Listbox(mid, height=14, selectmode="extended")
        self.listbox.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(mid)
        side.pack(side="left", fill="y", padx=6)
        for text, cmd in (("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1)),
                          ("Remove", self.remove), ("Clear", self.clear)):
            ttk.Button(side, text=text, command=cmd).pack(fill="x", pady=2)
        add = ttk.LabelFrame(self.top, text="Add to the list")
        add.pack(fill="x", **pad)
        ttk.Button(add, text="Add from post-import tasks file…", command=lambda: TasksDialog(app, self)).pack(
            side="left", padx=4, pady=4)
        ttk.Label(add, text="The D10-to-Central Migration Tool's <site>-agenda-center-post-import-tasks.json "
                            "(the .csv works too, minus the meetings to create by hand).",
                  foreground="#777").pack(side="left", padx=8)
        bot = ttk.Frame(self.top)
        bot.pack(fill="x", **pad)
        self.btn_run = ttk.Button(bot, text="Dry run the list", command=self.dry_run)
        self.btn_run.pack(side="left")
        ttk.Button(bot, text="Save list…", command=self.save_as).pack(side="left", padx=6)
        ttk.Button(bot, text="Load list…", command=self.load).pack(side="left")
        ttk.Button(bot, text="Undo a live run…", command=self.undo).pack(side="left", padx=16)
        ttk.Button(bot, text="Close", command=self.top.destroy).pack(side="right")
        self.refresh()

    # -------------------------------------------------------------- list
    def refresh(self) -> None:
        self.listbox.delete(0, "end")
        for i, s in enumerate(self.steps, 1):
            self.listbox.insert("end", f"{i}. {s.describe()}")
        self._persist()

    def _persist(self) -> None:
        from .agendacenter.plan import Plan
        try:
            Plan(name="To-do list", site_url=self.app.site.get(), steps=self.steps).save(TODO_FILE)
        except OSError:
            pass

    def add_steps(self, step_dicts: list[dict]) -> None:
        from .agendacenter.plan import Step
        self.steps += [Step.from_dict(d) for d in step_dicts]
        self.refresh()
        self.app.log(f"Added {len(step_dicts)} step(s) to the to-do list.")

    def move(self, d: int) -> None:
        sel = self.listbox.curselection()
        if len(sel) != 1:
            return
        i, j = sel[0], sel[0] + d
        if 0 <= j < len(self.steps):
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
            self.refresh()
            self.listbox.selection_set(j)

    def remove(self) -> None:
        for i in reversed(self.listbox.curselection()):
            del self.steps[i]
        self.refresh()

    def clear(self) -> None:
        if self.steps and messagebox.askyesno("To-do list", "Remove every step?", parent=self.top):
            self.steps.clear()
            self.refresh()

    def plan(self):
        from .agendacenter.plan import Plan
        return Plan(name="To-do list", site_url=self.app.site.get(), steps=list(self.steps))

    # --------------------------------------------------------------- run
    def dry_run(self) -> None:
        if not self.steps:
            messagebox.showinfo("To-do list", "The list is empty.", parent=self.top)
            return
        if self.app.worker.browser is None:
            messagebox.showinfo("To-do list", "Log in first (Open Chrome and log in).", parent=self.top)
            return
        plan = self.plan()
        try:
            plan.validate()
        except ValueError as e:
            messagebox.showerror("To-do list", str(e), parent=self.top)
            return
        what = "To-do list:\n" + "\n".join(f"{i}. {s.describe()}" for i, s in enumerate(plan.steps, 1))
        RunWindow(self.app, "Agenda Center: to-do list", what, lambda jobs: jobs.runner.run(plan))

    # -------------------------------------------------------------- files
    def save_as(self) -> None:
        if not self.steps:
            return
        folder = ROOT / "plans"
        folder.mkdir(exist_ok=True)
        p = filedialog.asksaveasfilename(parent=self.top, initialdir=str(folder), defaultextension=".json",
                                         initialfile="to-do list.json", filetypes=[("To-do list", "*.json")])
        if p:
            self.plan().save(Path(p))
            self.app.log(f"Saved the to-do list to {p}")

    def load(self) -> None:
        from .agendacenter.plan import Plan
        p = filedialog.askopenfilename(parent=self.top, initialdir=str(ROOT / "plans"),
                                       filetypes=[("To-do list", "*.json")])
        if not p:
            return
        try:
            plan = Plan.load(Path(p))
        except Exception as e:
            messagebox.showerror("Load list", str(e), parent=self.top)
            return
        self.steps = list(plan.steps)
        self.refresh()

    def undo(self) -> None:
        from .agendacenter.plan import undo_plan
        p = filedialog.askopenfilename(parent=self.top, initialdir=str(RUNS), title="undo.json from a live run",
                                       filetypes=[("Undo file", "undo.json")])
        if not p:
            return
        plan = undo_plan(Path(p))
        if not plan.steps:
            messagebox.showinfo("Undo", "That run has nothing to undo.", parent=self.top)
            return
        if self.steps and not messagebox.askyesno("Undo", "Replace the current list with the undo steps?",
                                                  parent=self.top):
            return
        self.steps = list(plan.steps)
        self.refresh()


class TasksDialog:
    """Pick a post-import tasks file and the files folder; adds the steps."""

    def __init__(self, app, todo: ToDoWindow) -> None:
        from .agendacenter.tasks import KIND_LABELS, KINDS
        self.app, self.todo = app, todo
        s = app.settings.get("post_import", {})
        self.top = tk.Toplevel(todo.top)
        self.top.title("Add post-import tasks")
        self.top.geometry("1000x430")
        self.top.transient(todo.top)
        pad = {"padx": 6, "pady": 3}
        f = ttk.Frame(self.top)
        f.pack(fill="x", **pad)
        self.tasks_file = tk.StringVar(value=s.get("tasks_file", ""))
        self.files_dir = tk.StringVar(value=s.get("files_dir", ""))
        for row, (label, var, cmd) in enumerate((
                ("Post-import tasks file (.json)", self.tasks_file, self.pick_tasks),
                ("Downloaded files folder", self.files_dir, self.pick_dir))):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="e", padx=4, pady=2)
            ttk.Entry(f, textvariable=var, width=95).grid(row=row, column=1, sticky="w")
            ttk.Button(f, text="Browse…", command=cmd).grid(row=row, column=2, padx=4)
        ttk.Label(f, text="Files are looked for in that folder, its <site>-agenda-center-attach-after-import "
                          "folder and anything below it.", foreground="#777").grid(row=2, column=1, sticky="w")
        self.summary = tk.StringVar(value="")
        ttk.Label(self.top, textvariable=self.summary, wraplength=950, justify="left").pack(fill="x", **pad)
        kinds = ttk.LabelFrame(self.top, text="Do")
        kinds.pack(fill="x", **pad)
        self.kind_vars = {}
        self.create_cats = tk.BooleanVar(value=True)
        ttk.Checkbutton(kinds, text="Create the categories in the file that aren't on the site (published)",
                        variable=self.create_cats).pack(anchor="w")
        for k in KINDS:
            v = tk.BooleanVar(value=True)
            self.kind_vars[k] = v
            label = KIND_LABELS[k]
            ttk.Checkbutton(kinds, text=label[0].upper() + label[1:], variable=v).pack(anchor="w")
        opts = ttk.LabelFrame(self.top, text="Options")
        opts.pack(fill="x", **pad)
        self.publish = tk.BooleanVar(value=bool(s.get("publish", True)))
        self.download = tk.BooleanVar(value=bool(s.get("download", False)))
        ttk.Checkbutton(opts, text="Publish new meetings and minutes (Notify Me is always answered Don't Send)",
                        variable=self.publish).pack(anchor="w")
        ttk.Checkbutton(opts, text="Download files that aren't in the folder from the old site",
                        variable=self.download).pack(anchor="w")
        bar = ttk.Frame(self.top)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text="Add to the to-do list", command=self.add).pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.top.destroy).pack(side="right")
        if self.tasks_file.get():
            self.load_summary()

    def pick_tasks(self) -> None:
        start = str(Path(self.tasks_file.get()).parent) if self.tasks_file.get() else None
        p = filedialog.askopenfilename(parent=self.top, filetypes=[("Post-import tasks", "*.json *.csv")],
                                       initialdir=start)
        if p:
            self.tasks_file.set(p)
            if not self.files_dir.get():
                self.files_dir.set(str(Path(p).parent))
            self.load_summary()

    def pick_dir(self) -> None:
        p = filedialog.askdirectory(parent=self.top, initialdir=self.files_dir.get() or None)
        if p:
            self.files_dir.set(p)

    def load_summary(self):
        from .agendacenter.tasks import KIND_LABELS, categories, counts, load_tasks
        try:
            tasks, warnings = load_tasks(Path(self.tasks_file.get()))
        except Exception as e:
            self.summary.set(f"Couldn't read the tasks file: {e}")
            return None
        c = counts(tasks)
        text = (f"{len(tasks)} meeting(s) in {len(categories(tasks))} categories: "
                + ", ".join(f"{c[k]} to {KIND_LABELS[k].split(' (')[0]}" for k in c) + ".")
        if warnings:
            text += "  " + " ".join(warnings)
        self.summary.set(text)
        return tasks

    def add(self) -> None:
        from .agendacenter.tasks import todo_steps
        tasks_file = Path(self.tasks_file.get())
        if not tasks_file.is_file():
            messagebox.showinfo("Post-import tasks", "Pick the tasks file first.", parent=self.top)
            return
        files_dir = Path(self.files_dir.get()) if self.files_dir.get().strip() else tasks_file.parent
        if not files_dir.is_dir():
            messagebox.showinfo("Post-import tasks", f"Folder not found: {files_dir}", parent=self.top)
            return
        tasks = self.load_summary()
        if tasks is None:
            return
        kinds = [k for k, v in self.kind_vars.items() if v.get()]
        steps = todo_steps(tasks_file, files_dir, tasks, publish=self.publish.get(),
                           download_missing=self.download.get(), create_categories=self.create_cats.get(),
                           kinds=kinds)
        if not steps:
            messagebox.showinfo("Post-import tasks", "Nothing to add with these choices.", parent=self.top)
            return
        self.app.settings["post_import"] = {"tasks_file": str(tasks_file), "files_dir": str(files_dir),
                                            "publish": self.publish.get(), "download": self.download.get()}
        self.app.save()
        self.todo.add_steps(steps)
        self.top.destroy()
