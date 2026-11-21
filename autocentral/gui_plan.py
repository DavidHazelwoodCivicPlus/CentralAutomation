"""The Plan tab, and the dialogs that add to it: find and replace in pages'
text blocks, move pages, move staff. Module publish / unpublish / delete
steps come from each module tab's "Add ticked to plan" menu.

The plan is kept in plans/current_plan.json, so it survives closing the
window, and can be saved under another name and opened again later.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from .cli import ROOT
from .editing import Rule, find_matches
from .gui_agenda import RUNS, RunWindow
from .plan_steps import (PlanJobs, Step, load_plan, move_pages_step, move_staff_step, replace_step, save_plan)

PLANS = ROOT / "plans"
CURRENT = PLANS / "current_plan.json"
KIND_NAMES = {"module_action": "Module", "move_pages": "Move pages", "move_staff": "Move staff", "replace": "Find and replace"}


class PlanTab:
    def __init__(self, app, nb: ttk.Notebook) -> None:
        self.app = app
        self.frame = ttk.Frame(nb)
        pad = {"padx": 4, "pady": 3}
        head = ttk.Frame(self.frame)
        head.pack(fill="x", **pad)
        ttk.Label(head, text="The plan: changes queued from the other tabs (Add ticked to plan, Move ticked…, "
                             "Find and replace…). It runs top to bottom, as a dry run first, then for real "
                             "once you type the site's address. One results list and one undo file for the whole run.",
                  wraplength=1100, justify="left").pack(side="left")
        mid = ttk.Frame(self.frame)
        mid.pack(fill="both", expand=True, **pad)
        cols = [("n", "#", 40), ("kind", "Kind", 120), ("title", "Step", 700), ("size", "How many", 90)]
        self.tree = ttk.Treeview(mid, columns=[c for c, _, _ in cols], show="headings", selectmode="extended")
        for c, label, w in cols:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, stretch=(c == "title"))
        ys = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        bar = ttk.Frame(self.frame)
        bar.pack(fill="x", **pad)
        self.buttons = []
        for text, cmd in (("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1)),
                          ("Remove", self.remove), ("Clear", self.clear), ("Open plan…", self.open),
                          ("Save plan as…", self.save_as)):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=3)
            self.buttons.append(b)
        self.btn_run = ttk.Button(bar, text="Run the plan… (dry run first)", command=self.run)
        self.btn_run.pack(side="right", padx=3)
        self.btn_undo = ttk.Button(bar, text="Undo a live run…", command=self.undo)
        self.btn_undo.pack(side="right", padx=3)
        self.buttons += [self.btn_run, self.btn_undo]
        self.steps: list[Step] = []
        try:
            if CURRENT.exists():
                self.steps = load_plan(CURRENT)
        except (OSError, ValueError):
            self.steps = []
        self.refresh()

    # ------------------------------------------------------------ the list
    @staticmethod
    def _size(st: Step) -> str:
        p = st.params
        if st.kind == "replace":
            return f"{len(p.get('targets', {}))} page(s)"
        if st.kind in ("move_pages", "move_staff"):
            return str(len(p.get("moves", [])))
        return str(len(p.get("categories", {})) + sum(len(v) for v in p.get("items", {}).values()))

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for n, st in enumerate(self.steps, 1):
            self.tree.insert("", "end", iid=st.id, values=[n, KIND_NAMES.get(st.kind, st.kind), st.title, self._size(st)])
        text = "Plan" + (f" ({len(self.steps)})" if self.steps else "")
        try:
            self.app.nb.tab(self.frame, text=text)
        except tk.TclError:
            pass
        try:
            save_plan(CURRENT, self.steps)
        except OSError:
            pass

    def add(self, step: Step) -> None:
        self.steps.append(step)
        self.refresh()
        self.app.log(f"Added to the plan: {step.title}")

    def _selected(self) -> list[int]:
        ids = set(self.tree.selection())
        return [i for i, s in enumerate(self.steps) if s.id in ids]

    def move(self, d: int) -> None:
        sel = self._selected()
        if not sel:
            return
        order = sel if d < 0 else list(reversed(sel))
        for i in order:
            j = i + d
            if 0 <= j < len(self.steps):
                self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
        keep = [self.steps[min(max(i + d, 0), len(self.steps) - 1)].id for i in sel]
        self.refresh()
        self.tree.selection_set(keep)

    def remove(self) -> None:
        sel = set(self._selected())
        self.steps = [s for i, s in enumerate(self.steps) if i not in sel]
        self.refresh()

    def clear(self) -> None:
        if self.steps and messagebox.askyesno("Plan", "Remove every step from the plan?"):
            self.steps = []
            self.refresh()

    def open(self) -> None:
        p = filedialog.askopenfilename(initialdir=str(PLANS), filetypes=[("Plan", "*.json")])
        if p:
            self.steps = load_plan(Path(p))
            self.refresh()

    def save_as(self) -> None:
        p = filedialog.asksaveasfilename(initialdir=str(PLANS), defaultextension=".json", filetypes=[("Plan", "*.json")])
        if p:
            save_plan(Path(p), self.steps)
            self.app.log(f"Saved the plan: {p}")

    # ------------------------------------------------------------ running
    def run(self) -> None:
        if not self.steps:
            messagebox.showinfo("Plan", "The plan is empty: add steps from the other tabs first.")
            return
        if self.app.worker.browser is None:
            messagebox.showinfo("Plan", "Log in first (Open Chrome and log in).")
            return
        steps = list(self.steps)
        what = f"Run the plan: {len(steps)} step(s): " + "; ".join(f"{n}. {s.title}" for n, s in enumerate(steps, 1))
        RunWindow(self.app, "Plan", what, lambda jobs: jobs.run(steps), factory=PlanJobs, module=None)

    def undo(self) -> None:
        import json
        if self.app.worker.browser is None:
            messagebox.showinfo("Plan", "Log in first (Open Chrome and log in).")
            return
        p = filedialog.askopenfilename(initialdir=str(RUNS), title="undo.json from a live run",
                                       filetypes=[("Undo file", "undo.json")])
        if not p:
            return
        entries = json.loads(Path(p).read_text(encoding="utf-8"))
        if not entries:
            messagebox.showinfo("Undo", "That run has nothing to undo.")
            return
        RunWindow(self.app, "Plan: undo a live run", f"Undo {Path(p).parent.name}: {len(entries)} change(s).",
                  lambda jobs: jobs.undo(entries), factory=PlanJobs, module=None)


# ============================================================ find and replace
class FindReplaceDialog:
    """Find in the read pages' text blocks, look at every match, tick the ones
    to change, add them to the plan."""

    def __init__(self, app) -> None:
        self.app = app
        self.tab = app.tabs["pages"]
        top = self.top = tk.Toplevel(app.root)
        top.title("Find and replace in pages")
        top.geometry("1250x650")
        pad = {"padx": 6, "pady": 3}
        form = ttk.Frame(top)
        form.pack(fill="x", **pad)
        ttk.Label(form, text="Find").grid(row=0, column=0, sticky="e")
        self.find = tk.StringVar()
        ttk.Entry(form, textvariable=self.find, width=50).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(form, text="Replace with").grid(row=1, column=0, sticky="e")
        self.repl = tk.StringVar()
        ttk.Entry(form, textvariable=self.repl, width=50).grid(row=1, column=1, sticky="w", padx=4)
        self.case, self.whole, self.regex, self.html = (tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar())
        for c, (t, v) in enumerate((("Match case", self.case), ("Whole words", self.whole), ("Regular expression", self.regex),
                                    ("Search the HTML (tags and addresses too)", self.html)), start=2):
            ttk.Checkbutton(form, text=t, variable=v).grid(row=0, column=c, sticky="w", padx=6)
        self.scope = tk.StringVar(value="shown")
        for c, (t, v) in enumerate((("All read pages", "all"), ("Pages shown in the table", "shown"),
                                    ("Ticked pages", "ticked")), start=2):
            ttk.Radiobutton(form, text=t, value=v, variable=self.scope).grid(row=1, column=c, sticky="w", padx=6)
        ttk.Button(form, text="Find matches", command=self.search).grid(row=0, column=6, rowspan=2, padx=10)
        self.info = tk.StringVar(value="Text mode changes only what visitors read; links, image addresses and tags "
                                       "are left alone. Tick HTML mode to change those too.")
        ttk.Label(top, textvariable=self.info, foreground="#555").pack(fill="x", **pad)
        mid = ttk.Frame(top)
        mid.pack(fill="both", expand=True, **pad)
        cols = [("page", "Page", 200), ("block", "Text block", 220), ("count", "#", 40), ("before", "Before", 340),
                ("after", "After", 340), ("shared", "Shared", 90)]
        self.tree = ttk.Treeview(mid, columns=[c for c, _, _ in cols], show="tree headings")
        self.tree.column("#0", width=40, stretch=False)
        for c, label, w in cols:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, stretch=c in ("before", "after"))
        ys = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._click)
        bar = ttk.Frame(top)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text="Tick all", command=lambda: self._tick_all(True)).pack(side="left")
        ttk.Button(bar, text="Untick all", command=lambda: self._tick_all(False)).pack(side="left", padx=4)
        ttk.Button(bar, text="Add ticked to the plan", command=self.add).pack(side="right")
        self.matches = []
        self.ticked: set[int] = set()

    def rule(self) -> Rule:
        return Rule(self.find.get(), self.repl.get(), in_html=self.html.get(), match_case=self.case.get(),
                    whole_word=self.whole.get(), regex=self.regex.get())

    def _pages(self) -> Optional[set[int]]:
        rows = self.tab.scan.rows
        if self.scope.get() == "all":
            return None
        if self.scope.get() == "ticked":
            return {rows[i].category_id for i in self.tab.checked if rows[i].level == "category"}
        return {rows[i].category_id for i in self.tab.shown if rows[i].level == "category"}

    def search(self) -> None:
        import re
        if not self.tab.scan or not self.find.get():
            return
        try:
            rule = self.rule()
            rule.pattern()
        except re.error as e:
            messagebox.showwarning("Find and replace", f"That regular expression isn't valid: {e}", parent=self.top)
            return
        self.matches = find_matches(self.tab.scan.rows, rule, self._pages())
        self.ticked = set(range(len(self.matches)))
        self.tree.delete(*self.tree.get_children())
        img = self.app.box_images
        for i, m in enumerate(self.matches):
            self.tree.insert("", "end", iid=str(i), image=img[True],
                             values=[m.page, m.block, m.count, m.before, m.after, m.shared])
        pages = len({m.page_id for m in self.matches})
        total = sum(m.count for m in self.matches)
        self.info.set(f"{total} match(es) in {len(self.matches)} text block(s) on {pages} page(s). Untick any to leave "
                      f"alone, then Add ticked to the plan." +
                      (" Some blocks are shared by several pages: changing one changes it everywhere." if any(m.shared for m in self.matches) else ""))

    def _click(self, e) -> Optional[str]:
        iid = self.tree.identify_row(e.y)
        if not iid or self.tree.identify_region(e.x, e.y) != "tree":
            return None
        i = int(iid)
        on = i not in self.ticked
        (self.ticked.add if on else self.ticked.discard)(i)
        self.tree.item(iid, image=self.app.box_images[on])
        return "break"

    def _tick_all(self, on: bool) -> None:
        self.ticked = set(range(len(self.matches))) if on else set()
        for iid in self.tree.get_children():
            self.tree.item(iid, image=self.app.box_images[on])

    def add(self) -> None:
        chosen = [m for i, m in enumerate(self.matches) if i in self.ticked]
        if not chosen:
            return
        status = {r.category_id: r.status for r in self.tab.scan.rows if r.level == "category"}
        targets: dict[int, dict] = {}
        for m in chosen:
            t = targets.setdefault(m.page_id, {"name": m.page, "published": status.get(m.page_id) == "published",
                                               "blocks": [], "positions": []})
            if m.block_id not in t["blocks"]:
                t["blocks"].append(m.block_id)
                t["positions"].append(m.position)
        self.app.plan.add(replace_step(self.rule(), targets))
        self.info.set(f"Added to the plan: {len(chosen)} text block(s) on {len(targets)} page(s).")


# ============================================================ moving things
class ChooseDialog:
    """Pick one place (a page or a department) from a filterable list."""

    def __init__(self, parent, title: str, choices: list[tuple[int, str]]) -> None:
        self.choice: Optional[tuple[int, str]] = None
        top = self.top = tk.Toplevel(parent)
        top.title(title)
        top.geometry("640x520")
        top.transient(parent)
        self.flt = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.flt)
        e.pack(fill="x", padx=8, pady=6)
        self.lb = tk.Listbox(top, activestyle="dotbox")
        self.lb.pack(fill="both", expand=True, padx=8)
        self.all = choices
        self.flt.trace_add("write", lambda *_: self._fill())
        self._fill()
        bar = ttk.Frame(top)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Choose", command=self._ok).pack(side="right")
        ttk.Button(bar, text="Cancel", command=top.destroy).pack(side="right", padx=6)
        self.lb.bind("<Double-1>", lambda _e: self._ok())
        e.focus_set()
        top.grab_set()
        parent.wait_window(top)

    def _fill(self) -> None:
        f = self.flt.get().lower()
        self.shown = [c for c in self.all if f in c[1].lower()]
        self.lb.delete(0, "end")
        for _, label in self.shown:
            self.lb.insert("end", label)

    def _ok(self) -> None:
        sel = self.lb.curselection()
        if sel:
            self.choice = self.shown[sel[0]]
        self.top.destroy()


def _descendants(rows, cid: int) -> set[int]:
    kids: dict[int, list[int]] = {}
    for r in rows:
        if r.level == "category" and r.extra.get("parent_id"):
            kids.setdefault(int(r.extra["parent_id"]), []).append(r.category_id)
    out, todo = set(), [cid]
    while todo:
        for k in kids.get(todo.pop(), []):
            if k not in out:
                out.add(k)
                todo.append(k)
    return out


def move_pages_dialog(app) -> None:
    tab = app.tabs["pages"]
    if not tab.scan:
        messagebox.showinfo("Move pages", "Read Pages first.")
        return
    rows = tab.scan.rows
    ticked = [rows[i] for i in sorted(tab.checked) if rows[i].level == "category"]
    ticked = [r for r in ticked if r.category_id != 1 and r.fields.get("Kind") != "layout page"]
    if not ticked:
        messagebox.showinfo("Move pages", "Tick the pages to move first (not the home page or layout pages).")
        return
    moving = {r.category_id for r in ticked}
    blocked = set(moving)
    for cid in moving:
        blocked |= _descendants(rows, cid)
    choices = [(r.category_id, f"{r.category}  (page {r.category_id}, {r.status})")
               for r in rows if r.level == "category" and r.category_id not in blocked and r.fields.get("Kind") != "layout page"]
    d = ChooseDialog(app.root, f"Move {len(ticked)} page(s) under…", choices)
    if not d.choice:
        return
    to, label = d.choice
    moves = [{"id": r.category_id, "name": r.name, "from": r.extra.get("parent_id"), "to": to,
              "to_name": label.split("  (")[0]} for r in ticked]
    app.plan.add(move_pages_step(moves, label.split("  (")[0]))


def move_staff_dialog(app) -> None:
    tab = app.tabs["staff"]
    if not tab.scan:
        messagebox.showinfo("Move staff", "Read Staff Directory first.")
        return
    rows = tab.scan.rows
    picked = [rows[i] for i in sorted(tab.checked)]
    depts = [r for r in picked if r.level == "category" and r.category_id]
    people = [r for r in picked if r.level == "item" and r.item_id and r.category_id not in {d.category_id for d in depts}]
    if not depts and not people:
        messagebox.showinfo("Move staff", "Tick the departments or employees to move first (read Staff Directory again "
                                          "if employees have no IDs).")
        return
    blocked = {d.category_id for d in depts}
    for d0 in depts:
        blocked |= _descendants(rows, d0.category_id)
    choices = [(r.category_id, r.category) for r in rows if r.level == "category" and r.category_id not in blocked]
    d = ChooseDialog(app.root, f"Move {len(depts)} department(s) and {len(people)} employee(s) to…", choices)
    if not d.choice:
        return
    to, label = d.choice
    moves = [{"kind": "department", "id": r.category_id, "name": r.name, "from": r.extra.get("parent_id") or 0,
              "to": to, "to_name": label} for r in depts]
    moves += [{"kind": "employee", "id": r.item_id, "name": r.name, "from": r.category_id, "to": to, "to_name": label}
              for r in people if r.category_id != to]
    if moves:
        app.plan.add(move_staff_step(moves, label))
