"""The action bar in every module tab except Agenda Center (gui_agenda.py):
publish, unpublish and delete the ticked rows, delete everything in the
module, create categories, and undo a live run.

Same run window and the same safety as Agenda Center: a dry run first, the
site's address typed for a live run, results.csv and undo.json per live run,
Notify Me always answered Don't Send.
"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from .actions.jobs import WORDS, ModuleJobs, Selection
from .gui_agenda import RUNS, RunWindow


def ask_names(parent, module_label: str, cap: Optional[int] = None) -> Optional[tuple[list[str], bool]]:
    """A small window for category names, one per line. Returns (names, publish)."""
    top = tk.Toplevel(parent)
    top.title(f"{module_label}: create categories")
    top.geometry("520x420")
    top.transient(parent)
    ttk.Label(top, text="One name per line. Names already on the site are skipped."
                        + (f" Calendar names: {cap} characters at most." if cap else ""),
              wraplength=500).pack(fill="x", padx=8, pady=6)
    text = tk.Text(top, height=14, wrap="none")
    text.pack(fill="both", expand=True, padx=8)
    publish = tk.BooleanVar(value=True)
    ttk.Checkbutton(top, text="Publish them", variable=publish).pack(anchor="w", padx=8, pady=4)
    out: dict = {}

    def ok() -> None:
        names = [n.strip() for n in text.get("1.0", "end").splitlines() if n.strip()]
        if names:
            out["v"] = (names, publish.get())
        top.destroy()
    bar = ttk.Frame(top)
    bar.pack(fill="x", padx=8, pady=6)
    ttk.Button(bar, text="Dry run…", command=ok).pack(side="right")
    ttk.Button(bar, text="Cancel", command=top.destroy).pack(side="right", padx=6)
    text.focus_set()
    top.grab_set()
    parent.wait_window(top)
    return out.get("v")


class ModuleActions:
    """The action bar inside one module tab."""

    def __init__(self, app, tab) -> None:
        self.app, self.tab, self.key = app, tab, tab.m.key
        self.cw, self.iw = WORDS.get(self.key, ("category", "item"))
        box = ttk.LabelFrame(tab.frame, text=f"{tab.m.label} actions")
        box.pack(fill="x", padx=4, pady=3, after=tab.head)
        self.buttons = [
            ttk.Button(box, text="Publish ticked…", command=lambda: self.ticked("publish")),
            ttk.Button(box, text="Unpublish ticked…", command=lambda: self.ticked("unpublish")),
            ttk.Button(box, text="Delete ticked…", command=lambda: self.ticked("delete")),
        ]
        if self.key != "pages":          # never a whole site's pages in one click
            self.buttons.append(ttk.Button(box, text="Delete everything in this module…", command=self.everything))
        if self.key not in ("resdir", "pages", "docs"):
            self.buttons.append(ttk.Button(box, text="Create categories…", command=self.create))
        self.buttons.append(ttk.Button(box, text="Undo a live run…", command=self.undo))
        plan = ttk.Menubutton(box, text="Add ticked to plan ▾")
        menu = tk.Menu(plan, tearoff=False)
        for action in ("publish", "unpublish", "delete"):
            menu.add_command(label=action.capitalize(), command=lambda a=action: self.to_plan(a))
        plan["menu"] = menu
        self.buttons.append(plan)
        for b in self.buttons:
            b.pack(side="left", padx=4, pady=3)
        note = ("Every action runs as a dry run first. Ticking a page ticks the rows under it, sub-pages "
                "included; sub-pages are deleted before their parents. The home page and layout pages are "
                "never unpublished or deleted." if self.key == "pages" else
                "Every action runs as a dry run first." if self.key == "resdir" else
                "Every action runs as a dry run first. A ticked folder means the whole folder: deleting it deletes "
                "its files and sub-folders too. The two top folders (Document Center, Public Images) are never "
                "changed." if self.key == "docs" else
                f"Every action runs as a dry run first. A ticked {self.cw} means the whole {self.cw} "
                f"(for Delete: with every {self.iw} in it).")
        ttk.Label(box, text=note, foreground="#777", wraplength=360, justify="left").pack(side="left", padx=10)

    @property
    def label(self) -> str:
        return self.tab.scan.label if self.tab.scan and self.tab.scan.label else self.tab.m.label

    def _logged_in(self) -> bool:
        if self.app.worker.browser is None:
            messagebox.showinfo(self.label, "Log in first (Open Chrome and log in).")
            return False
        return True

    def _factory(self):
        key = self.key
        return lambda browser, rep, live: ModuleJobs(browser, rep, live, key)

    def _window(self, title: str, what: str, job) -> RunWindow:
        return RunWindow(self.app, f"{self.label}: {title}", what, job, factory=self._factory(), module=self.key)

    # ------------------------------------------------------------- actions
    def ticked(self, action: str) -> None:
        if not self._logged_in():
            return
        sel = Selection.from_rows(self.key, self.tab.ticked_rows())
        if not sel.categories and not sel.item_count():
            messagebox.showinfo(self.label, "Tick the rows first.")
            return
        names = ", ".join(list(sel.categories.values())[:5]) + (" …" if len(sel.categories) > 5 else "")
        what = f"{action.capitalize()} {sel.describe()}" + (f" ({self.cw}s: {names})" if names else "") + "."
        if action == "delete" and sel.categories:
            what += f" Each {self.cw} is deleted with every {self.iw} in it"
            what += " and every department inside it." if self.key == "staff" else "."
        if action == "publish":
            if sel.categories:
                what += f" Each ticked {self.cw} is published with every unpublished {self.iw} in it."
            what += " Notify Me is answered Don't Send."
        if action == "delete":
            job = lambda jobs: jobs.delete(sel)
        else:
            job = lambda jobs, pub=(action == "publish"): jobs.set_published(sel, pub)
        self._window(f"{action} ticked", what, job)

    def to_plan(self, action: str) -> None:
        """Queue publish / unpublish / delete of the ticked rows in the Plan tab instead of running it now."""
        from .plan_steps import module_step
        sel = Selection.from_rows(self.key, self.tab.ticked_rows())
        if not sel.categories and not sel.item_count():
            messagebox.showinfo(self.label, "Tick the rows first.")
            return
        if sel.unresolved:
            messagebox.showinfo(self.label, "Some ticked employees have no IDs (an old read): read Staff Directory again first.")
            return
        self.app.plan.add(module_step(self.key, self.label, action, sel))

    def everything(self) -> None:
        if not self._logged_in():
            return
        what = (f"Delete EVERYTHING in {self.label}: every {self.iw}." if self.key == "resdir" else
                f"Delete EVERYTHING in {self.label}: every {self.cw} with every {self.iw} in it.")
        self._window("delete everything", what, lambda jobs: jobs.delete_everything())

    def create(self) -> None:
        if not self._logged_in():
            return
        got = ask_names(self.app.root, self.label, cap=50 if self.key == "calendar" else None)
        if not got:
            return
        names, publish = got
        what = (f"Create {len(names)} {self.cw}{'' if len(names) == 1 else 's'}"
                + (" (published)" if publish else " (unpublished)") + ": " + ", ".join(names[:8])
                + (" …" if len(names) > 8 else "") + ".")
        self._window("create categories", what, lambda jobs: jobs.create_categories(names, publish))

    def undo(self) -> None:
        if not self._logged_in():
            return
        p = filedialog.askopenfilename(initialdir=str(RUNS), title="undo.json from a live run",
                                       filetypes=[("Undo file", "undo.json")])
        if not p:
            return
        entries = [e for e in json.loads(Path(p).read_text(encoding="utf-8")) if e.get("module") == self.key]
        if not entries:
            messagebox.showinfo("Undo", f"That run has nothing to undo in {self.label}.")
            return
        counts: dict[str, int] = {}
        for e in entries:
            counts[e["undo"]] = counts.get(e["undo"], 0) + 1
        what = ("Undo " + Path(p).parent.name + ": " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ".")
        self._window("undo a live run", what, lambda jobs: jobs.undo(entries))
