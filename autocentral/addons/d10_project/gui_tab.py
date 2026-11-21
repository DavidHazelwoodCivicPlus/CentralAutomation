"""The D10 project tab: pick the exports folder, plan (no site), then run
the steps on the logged-in site; a checklist shows each step, the log what
it's doing, and the tracking workbook opens at the end."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .project import Options, Project, default_work
from .runner import SITE_STEPS, STEPS, ProjectRun

ZONES = ["Eastern", "Central", "Mountain", "Arizona", "Pacific", "Alaska", "Hawaii"]


class ProjectTab:
    def __init__(self, app, nb: ttk.Notebook) -> None:
        self.app = app
        self.frame = ttk.Frame(nb)
        self.q: "queue.Queue[tuple]" = queue.Queue()
        s = (app.settings.get("d10_project") or {}) if hasattr(app, "settings") else {}
        pad = {"padx": 4, "pady": 3}
        top = ttk.Frame(self.frame)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="D10 exports folder:").grid(row=0, column=0, sticky="w")
        self.exports = tk.StringVar(value=s.get("exports", ""))
        ttk.Entry(top, textvariable=self.exports, width=70).grid(row=0, column=1, sticky="we")
        ttk.Button(top, text="Choose…", command=self.choose).grid(row=0, column=2, **pad)
        ttk.Label(top, text="Put the pages under:").grid(row=1, column=0, sticky="w")
        self.root_page = tk.StringVar(value=s.get("root_page", "Home"))
        ttk.Entry(top, textvariable=self.root_page, width=30).grid(row=1, column=1, sticky="w")
        ttk.Label(top, text="Time zone:").grid(row=2, column=0, sticky="w")
        self.zone = tk.StringVar(value=s.get("zone", "Central"))
        ttk.Combobox(top, textvariable=self.zone, values=ZONES, width=12, state="readonly").grid(row=2, column=1, sticky="w")
        self.publish = tk.BooleanVar(value=s.get("publish", True))
        ttk.Checkbutton(top, text="Publish pages that were published on the D10 site", variable=self.publish).grid(
            row=3, column=1, sticky="w")
        self.empty_agendas = tk.BooleanVar(value=False)            # never remembered: asked each time
        ttk.Checkbutton(top, text="Empty the site's Agenda Center before its import (asks again)",
                        variable=self.empty_agendas).grid(row=4, column=1, sticky="w")
        top.columnconfigure(1, weight=1)
        self.summary = tk.StringVar(value="Choose the folder with the www-*.json exports.")
        ttk.Label(self.frame, textvariable=self.summary, foreground="#444").pack(fill="x", **pad)

        btns = ttk.Frame(self.frame)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="1. Plan (no site changes)", command=self.plan).pack(side="left", padx=2)
        ttk.Button(btns, text="2. Run all steps on the site…", command=self.run_all).pack(side="left", padx=2)
        ttk.Button(btns, text="Run ticked steps…", command=self.run_ticked).pack(side="left", padx=2)
        ttk.Button(btns, text="Check the site against the D10 files", command=self.check).pack(side="left", padx=2)
        ttk.Button(btns, text="Stop", command=self.stop).pack(side="left", padx=2)
        ttk.Button(btns, text="Tracking workbook", command=self.open_tracking).pack(side="right", padx=2)
        ttk.Button(btns, text="Work folder", command=self.open_work).pack(side="right", padx=2)

        pane = ttk.PanedWindow(self.frame, orient="vertical")
        pane.pack(fill="both", expand=True, **pad)
        self.tree = ttk.Treeview(pane, columns=("tick", "state", "when", "note"), show="tree headings", height=len(STEPS))
        for c, t, w in (("#0", "Step", 300), ("tick", "Run", 40), ("state", "State", 80), ("when", "When", 130),
                        ("note", "Note", 600)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=(c == "note"))
        for k, label in STEPS:
            self.tree.insert("", "end", iid=k, text=label, values=("☐", "", "", ""))
        self.tree.bind("<Button-1>", self._click)
        pane.add(self.tree, weight=1)
        self.text = tk.Text(pane, height=12, wrap="word")
        pane.add(self.text, weight=2)
        self.running = False
        self.frame.after(300, self._poll)
        self.refresh()

    # ------------------------------------------------------------ helpers
    @property
    def work(self) -> Path:
        return default_work(Path(self.exports.get())) if self.exports.get() else Path()

    def _save(self) -> None:
        if hasattr(self.app, "settings"):
            self.app.settings["d10_project"] = {"exports": self.exports.get(), "root_page": self.root_page.get(),
                                                "zone": self.zone.get(), "publish": self.publish.get()}
            try:
                from ...gui import save_settings
                save_settings(self.app.settings)
            except Exception:
                pass

    def options(self) -> Options:
        return Options(root_page=self.root_page.get().strip() or "Home", publish_pages=self.publish.get(),
                       zone=self.zone.get(), agenda_empty_first=self.empty_agendas.get())

    def say(self, msg: str) -> None:
        self.q.put(("log", msg))

    def choose(self) -> None:
        p = filedialog.askdirectory(title="The folder with the D10 www-*.json exports")
        if p:
            self.exports.set(p)
            self._save()
            self.refresh()

    def _click(self, e) -> None:
        if self.tree.identify_column(e.x) == "#1":
            row = self.tree.identify_row(e.y)
            if row:
                v = list(self.tree.item(row, "values"))
                v[0] = "☑" if v[0] == "☐" else "☐"
                self.tree.item(row, values=v)

    def ticked(self) -> list[str]:
        return [k for k, _ in STEPS if self.tree.item(k, "values")[0] == "☑"]

    def refresh(self) -> None:
        pr = Project.load(self.work) if self.exports.get() else None
        for k, _ in STEPS:
            st = (pr.steps.get(k) if pr else None) or {}
            tick = self.tree.item(k, "values")[0]
            self.tree.item(k, values=(tick, st.get("state", ""), st.get("at", ""), st.get("note", "")))
        if pr:
            c = pr.counts()
            self.summary.set(f"{pr.site} ({pr.old_host}): " + ", ".join(f"{v} {k}" for k, v in c.items())
                             + (f"  ·  going to {pr.central}" if pr.central else ""))
        elif self.exports.get():
            n = len(list(Path(self.exports.get()).glob("*.json")))
            self.summary.set(f"{n} export file(s). Click Plan to see what goes where (nothing on any site changes).")

    def _poll(self) -> None:
        try:
            while True:
                ev = self.q.get_nowait()
                if ev[0] == "log":
                    self.text.insert("end", ev[1] + "\n")
                    self.text.see("end")
                elif ev[0] == "step":
                    v = list(self.tree.item(ev[1], "values"))
                    st = ev[2]
                    if st.startswith("working: "):          # a part of a long step (Agenda Center)
                        v[1], v[3] = "working…", st[9:]
                    else:
                        v[1] = {"working": "working…", "done": "✓ done", "failed": "✗ failed"}.get(st, st)
                    self.tree.item(ev[1], values=v)
                    if not ev[2].startswith("working"):
                        self.refresh()
                elif ev[0] == "end":
                    self.running = False
                    self.refresh()
        except queue.Empty:
            pass
        self.frame.after(300, self._poll)

    # ------------------------------------------------------------ runs
    def _start(self, steps: list[str], live: bool, read_only: bool = False) -> None:
        if self.running:
            messagebox.showinfo("D10 project", "A run is already going.")
            return
        ex = Path(self.exports.get())
        if not self.exports.get() or not list(ex.glob("*.json")):
            messagebox.showinfo("D10 project", "Choose the folder with the D10 www-*.json exports first.")
            return
        self._save()
        browser = None
        if live:
            browser = getattr(getattr(self.app, "worker", None), "browser", None)
            if browser is None:
                messagebox.showinfo("D10 project", "Open Chrome and log in to the Central site first (top of the window).")
                return
            from ...gui_agenda import confirm_site
            if not read_only and not confirm_site(self.frame, browser.host,
                                f"Run these steps of the D10 project on {browser.host}:\n  "
                                + "\n  ".join(lbl for k, lbl in STEPS if k in steps)
                                + "\n\nNothing is deleted and subscribers are never notified."):
                return
            if self.empty_agendas.get() and "agendas" in steps and not messagebox.askyesno(
                    "Empty the Agenda Center", f"EVERYTHING in the Agenda Center of {browser.host} is deleted before the "
                    "import: every category, agenda and item. This can't be undone.\n\nGo ahead?", icon="warning",
                    parent=self.frame):
                return
            from ...reporting import CancelToken
            self.cancel = CancelToken()
            browser.cancel = self.cancel
        self.running = True
        run = ProjectRun(ex, self.work, browser, self.say, self.options(),
                         on_step=lambda k, s: self.q.put(("step", k, s)))

        def go():
            try:
                run.run(steps)
                self.say("Finished." if not (browser and browser.cancel.cancelled) else "Stopped.")
            except Exception as e:
                self.say(f"Stopped at an error: {e}")
            finally:
                self.q.put(("end",))
        threading.Thread(target=go, daemon=True).start()

    def plan(self) -> None:
        self._start(["plan", "download", "report"], live=False)

    def run_all(self) -> None:
        self._start([k for k, _ in STEPS], live=True)

    def run_ticked(self) -> None:
        steps = self.ticked()
        if not steps:
            messagebox.showinfo("D10 project", "Tick the steps to run in the Run column.")
            return
        self._start(steps, live=bool(set(steps) & SITE_STEPS))

    def check(self) -> None:
        """Read only: what's missing, extra or twice on the site, written to the
        tracking workbook's Check sheet."""
        self._start(["check", "report"], live=True, read_only=True)

    def stop(self) -> None:
        c = getattr(self, "cancel", None)
        if c is not None:
            c.cancel()
            self.say("Stopping after the current request…")

    def open_tracking(self) -> None:
        pr = Project.load(self.work) if self.exports.get() else None
        p = self.work / f"{pr.site}-project-tracking.xlsx" if pr else None
        if not p or not p.is_file():
            messagebox.showinfo("D10 project", "No tracking workbook yet: it's written by Plan and at the end of each run.")
            return
        os.startfile(p)

    def open_work(self) -> None:
        if self.work and self.work.is_dir():
            os.startfile(self.work)
