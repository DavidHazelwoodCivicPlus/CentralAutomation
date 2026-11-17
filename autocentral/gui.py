"""The window (Tkinter): an Overview tab plus one tab per module.

Each module tab shows that module's categories and items as a tree, with its
own filters, its own columns and a tick box on every row. The ticks don't do
anything yet: they are what later actions (delete, publish ...) will act on.

One background thread owns the browser; the window talks to it through
queues, so the window never freezes while a module is read. The last read of
every module is saved per site and shown again when the window reopens.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional
from urllib.parse import urlparse

from .browser import Browser
from .cli import ROOT, cache_dir, default_out
from .inventory import RowFilter, export_xlsx
from .models import InventoryRow, ModuleScan
from .modules import MODULES, ModuleDef
from .reporting import CancelToken, Cancelled, Reporter, new_run_dir
from .scanner import Scanner
from .store import load_all, save_scan

SETTINGS = ROOT / "settings.json"
ANY = "(any)"
NO_INFO = "(no info)"
DATE_HINT = "YYYY-MM-DD or YYYY-MM-DD HH:MM"

# Columns every module tab has, before and after the module's own ones.
BASE_COLS = [("status", "Status", 90), ("created_on", "Created", 135), ("created_by", "Created By", 130),
             ("modified_on", "Last Modified", 120), ("modified_by", "Modified By", 120)]
TAIL_COLS = [("notes", "Notes", 220), ("id", "ID", 60)]


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def host_of(site: str) -> str:
    site = site.strip()
    if site and not re.match(r"^https?://", site):
        site = "https://" + site
    return urlparse(site).netloc.lower()


def sort_value(v) -> tuple:
    """Blanks last, numbers as numbers, everything else as text (dates are
    YYYY-MM-DD so they sort as text)."""
    v = "" if v is None else str(v)
    if not v:
        return (1, 0, 0, "")
    if re.fullmatch(r"-?\d+", v):
        return (0, 0, int(v), "")
    return (0, 1, 0, v.lower())


def _box_image(checked: bool) -> tk.PhotoImage:
    """A small tick box drawn pixel by pixel (Treeview has no checkbox)."""
    img = tk.PhotoImage(width=15, height=13)
    img.put("#1a73e8" if checked else "#ffffff", to=(1, 0, 14, 13))
    edge = "#1a73e8" if checked else "#777777"
    img.put(edge, to=(1, 0, 14, 1))
    img.put(edge, to=(1, 12, 14, 13))
    img.put(edge, to=(1, 0, 2, 13))
    img.put(edge, to=(13, 0, 14, 13))
    if checked:
        for x, y in ((4, 6), (5, 7), (6, 8), (7, 7), (8, 6), (9, 5), (10, 4), (11, 3)):
            img.put("#ffffff", to=(x, y, x + 1, y + 2))
    return img


class Worker(threading.Thread):
    """Runs browser jobs one at a time and posts events back to the window."""

    def __init__(self, events: "queue.Queue[tuple]") -> None:
        super().__init__(daemon=True)
        self.jobs: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self.events = events
        self.browser: Optional[Browser] = None
        self.cancel = CancelToken()

    def run(self) -> None:
        while True:
            job = self.jobs.get()
            self.cancel = CancelToken()
            if self.browser is not None:
                self.browser.cancel = self.cancel
            try:
                job()
            except Cancelled:
                self.events.put(("log", "warn", "Stopped."))
            except Exception as e:      # report, keep the worker alive
                self.events.put(("log", "error", f"{type(e).__name__}: {e}"))
            finally:
                self.events.put(("idle",))

    def reporter(self) -> Reporter:
        return Reporter(on_log=lambda level, msg: self.events.put(("log", level, msg)))


# ====================================================================== tabs
class ModuleTab:
    """One module: its tree of categories and items, filters and ticks."""

    def __init__(self, app: "App", nb: ttk.Notebook, m: ModuleDef) -> None:
        self.app, self.m = app, m
        self.frame = ttk.Frame(nb)
        self.scan: Optional[ModuleScan] = None
        self.checked: set[int] = set()          # indexes into scan.rows
        self.parent: dict[int, Optional[int]] = {}
        self.shown: list[int] = []              # rows in the tree, in tree order
        self.matched: set[int] = set()
        self.sort_key: Optional[str] = None
        self.sort_desc = False
        self.field_cols: list[str] = []
        self.reading = False
        self._build()

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        f = self.frame
        pad = {"padx": 4, "pady": 3}
        head = self.head = ttk.Frame(f)
        head.pack(fill="x", **pad)
        self.info = tk.StringVar(value="Not read yet. Click 'Read this module'.")
        ttk.Label(head, textvariable=self.info).pack(side="left")
        self.btn_read = ttk.Button(head, text="Read this module", command=lambda: self.app.read([self.m.key]))
        self.btn_read.pack(side="right")
        ttk.Button(head, text="Export shown rows…", command=self.export).pack(side="right", padx=4)

        flt = ttk.LabelFrame(f, text="Filter")
        flt.pack(fill="x", **pad)
        ttk.Label(flt, text="Status").grid(row=0, column=0, sticky="e", padx=(2, 2))
        self.f_status = tk.StringVar(value=ANY)
        self.cb_status = ttk.Combobox(flt, textvariable=self.f_status, values=[ANY], width=14, state="readonly")
        self.cb_status.grid(row=0, column=1, sticky="w")
        ttk.Label(flt, text="Created by").grid(row=0, column=2, sticky="e", padx=(10, 2))
        self.f_by = tk.StringVar(value=ANY)
        self.cb_by = ttk.Combobox(flt, textvariable=self.f_by, values=[ANY, NO_INFO], width=24, state="readonly")
        self.cb_by.grid(row=0, column=3, sticky="w")
        ttk.Label(flt, text="Created from").grid(row=0, column=4, sticky="e", padx=(10, 2))
        self.f_from = tk.StringVar()
        ttk.Entry(flt, textvariable=self.f_from, width=17).grid(row=0, column=5)
        ttk.Label(flt, text="to").grid(row=0, column=6, padx=2)
        self.f_to = tk.StringVar()
        ttk.Entry(flt, textvariable=self.f_to, width=17).grid(row=0, column=7)
        self.f_unknown = tk.BooleanVar(value=True)
        ttk.Checkbutton(flt, text="Also show records with no created info", variable=self.f_unknown,
                        command=self.refresh).grid(row=0, column=8, padx=(10, 0), sticky="w")

        ttk.Label(flt, text="Search").grid(row=1, column=0, sticky="e", pady=3)
        self.f_text = tk.StringVar()
        ttk.Entry(flt, textvariable=self.f_text, width=40).grid(row=1, column=1, columnspan=3, sticky="w")
        self.f_regex = tk.BooleanVar()
        ttk.Checkbutton(flt, text="regex", variable=self.f_regex, command=self.refresh).grid(
            row=1, column=4, sticky="w", padx=(10, 0))
        ttk.Label(flt, text=f"dates: {DATE_HINT}", foreground="#777").grid(row=1, column=5, columnspan=3, sticky="w")
        self.f_ticked = tk.BooleanVar()
        ttk.Checkbutton(flt, text="Ticked only", variable=self.f_ticked, command=self.refresh).grid(
            row=1, column=8, sticky="w", padx=(10, 0))
        ttk.Button(flt, text="Clear filters", command=self.clear_filters).grid(row=1, column=9, padx=6)
        for cb in (self.cb_status, self.cb_by):
            cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        for v in (self.f_from, self.f_to, self.f_text):
            v.trace_add("write", lambda *_: self.refresh())

        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, **pad)
        self.tree = ttk.Treeview(mid, show="tree headings", selectmode="extended")
        ys = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.tree.tag_configure("category", background="#eef2f7")
        self.tree.tag_configure("context", foreground="#999999")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)
        self._set_columns([])

        bar = ttk.Frame(f)
        bar.pack(fill="x", **pad)
        self.count = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.count).pack(side="left")
        ttk.Label(bar, text="Tick a row with its box or the space bar; a category ticks what's shown under it.",
                  foreground="#777").pack(side="left", padx=12)
        ttk.Button(bar, text="Collapse all", command=lambda: self._open_all(False)).pack(side="right")
        ttk.Button(bar, text="Expand all", command=lambda: self._open_all(True)).pack(side="right", padx=4)
        ttk.Button(bar, text="Untick all", command=self.untick_all).pack(side="right", padx=(12, 0))
        ttk.Button(bar, text="Tick shown", command=self.tick_shown).pack(side="right", padx=4)

    def _set_columns(self, fields: list[str]) -> None:
        self.field_cols = fields
        cols = [k for k, _, _ in BASE_COLS] + [f"f{i}" for i in range(len(fields))] + [k for k, _, _ in TAIL_COLS]
        self.tree.configure(columns=cols)
        self.tree.heading("#0", text="Name", command=lambda: self.sort_by("name"))
        self.tree.column("#0", width=380, stretch=True)
        specs = BASE_COLS + [(f"f{i}", label, 120) for i, label in enumerate(fields)] + TAIL_COLS
        for k, label, width in specs:
            self.tree.heading(k, text=label, command=lambda k=k: self.sort_by(k))
            self.tree.column(k, width=width, stretch=False)

    # ------------------------------------------------------------------ data
    def set_scan(self, ms: Optional[ModuleScan]) -> None:
        self.scan = ms
        self.checked.clear()
        self.reading = False
        rows = ms.rows if ms else []
        fields: list[str] = []
        for r in rows:                  # the module's own columns, skipping ones empty everywhere
            for k, v in r.fields.items():
                if v and k not in fields:
                    fields.append(k)
        self._set_columns(fields)
        self.parent = self._parents(rows)
        statuses = sorted({r.status for r in rows if r.status})
        people = sorted({r.created_by for r in rows if r.created_by})
        self.cb_status.configure(values=[ANY] + statuses)
        self.cb_by.configure(values=[ANY, NO_INFO] + people)
        if self.f_status.get() not in [ANY] + statuses:
            self.f_status.set(ANY)
        if self.f_by.get() not in [ANY, NO_INFO] + people:
            self.f_by.set(ANY)
        self._update_info()
        self.refresh()

    def set_reading(self) -> None:
        self.reading = True
        self._update_info()

    def _update_info(self) -> None:
        ms = self.scan
        prefix = "Reading…  " if self.reading else ""
        if ms is None:
            self.info.set(prefix or "Not read yet. Click 'Read this module'.")
            return
        if not ms.present:
            self.info.set(prefix + f"Not on this site (checked {ms.read_at or 'earlier'}).")
            return
        cats = sum(1 for r in ms.rows if r.level == "category")
        known = sum(1 for r in ms.rows if r.created_by)
        text = (f"Read {ms.read_at or 'earlier'}: {cats} categor{'y' if cats == 1 else 'ies'}, "
                f"{len(ms.rows) - cats} item(s); created info known for {known} of {len(ms.rows)}.")
        if ms.problems:
            text += f"  {len(ms.problems)} note(s) in the Overview tab."
        self.info.set(prefix + text)

    @staticmethod
    def _parents(rows: list[InventoryRow]) -> dict[int, Optional[int]]:
        """Each row's parent row: an item's category; a Staff department's
        parent department. Employees have no ids, so they go under the
        department with their department's name."""
        cat_by_id = {r.category_id: i for i, r in enumerate(rows)
                     if r.level == "category" and r.category_id is not None}
        cat_by_name: dict[str, int] = {}
        for i, r in enumerate(rows):
            if r.level == "category":
                cat_by_name.setdefault(r.name, i)
        out: dict[int, Optional[int]] = {}
        for i, r in enumerate(rows):
            if r.level == "category":
                p = cat_by_id.get(r.extra.get("parent_id"))
                out[i] = p if p != i else None
            elif r.category_id is not None:
                out[i] = cat_by_id.get(r.category_id)
            else:
                out[i] = cat_by_name.get(r.category) if r.category else None
        return out

    def current_filter(self) -> RowFilter:
        by = self.f_by.get()
        return RowFilter(statuses=[] if self.f_status.get() == ANY else [self.f_status.get()],
                         created_by=[] if by == ANY else ["unknown" if by == NO_INFO else by],
                         created_from=self.f_from.get().strip(), created_to=self.f_to.get().strip(),
                         include_unknown=self.f_unknown.get(),
                         text=self.f_text.get().strip(), regex=self.f_regex.get())

    def filters_active(self) -> bool:
        return bool(self.f_status.get() != ANY or self.f_by.get() != ANY or self.f_from.get().strip()
                    or self.f_to.get().strip() or self.f_text.get().strip() or self.f_ticked.get())

    def clear_filters(self) -> None:
        self.f_status.set(ANY)
        self.f_by.set(ANY)
        for v in (self.f_from, self.f_to, self.f_text):
            v.set("")
        self.f_regex.set(False)
        self.f_ticked.set(False)
        self.f_unknown.set(True)
        self.refresh()

    def sort_by(self, key: str) -> None:
        self.sort_desc = not self.sort_desc if self.sort_key == key else False
        self.sort_key = key
        self.refresh()

    def _value(self, r: InventoryRow, key: str):
        if key == "name":
            return r.name
        if key == "id":
            return r.own_id
        if key == "notes":
            return self._notes(r)
        if key.startswith("f") and key[1:].isdigit():
            return r.fields.get(self.field_cols[int(key[1:])], "")
        return getattr(r, key, "")

    @staticmethod
    def _notes(r: InventoryRow) -> str:
        if "history_starts" in r.extra:
            return f"History starts {r.extra['history_starts']}"
        return ""

    def refresh(self) -> None:
        tree = self.tree
        tree.delete(*tree.get_children())
        self.shown = []
        rows = self.scan.rows if self.scan else []
        flt = self.current_filter()
        ticked_only = self.f_ticked.get()
        self.matched = {i for i, r in enumerate(rows) if flt.match(r) and (not ticked_only or i in self.checked)}
        visible = set(self.matched)
        for i in self.matched:         # keep the categories above a match, greyed
            p, hops = self.parent.get(i), 0
            while p is not None and hops < 50:
                visible.add(p)
                p, hops = self.parent.get(p), hops + 1
        children: dict[Optional[int], list[int]] = {}
        for i in range(len(rows)):
            if i in visible:
                children.setdefault(self.parent.get(i), []).append(i)
        if self.sort_key:
            for lst in children.values():
                lst.sort(key=lambda i: sort_value(self._value(rows[i], self.sort_key)), reverse=self.sort_desc)
        items = sum(1 for i in visible if rows[i].level == "item")
        open_all = self.filters_active() or items <= 400
        img = self.app.box_images

        def add(parent_iid: str, i: int) -> None:
            r = rows[i]
            tags = [r.level] + ([] if i in self.matched else ["context"])
            values = [self._value(r, k) for k, _, _ in BASE_COLS] + \
                     [r.fields.get(f, "") for f in self.field_cols] + \
                     [self._notes(r), r.own_id]
            tree.insert(parent_iid, "end", iid=str(i), text=" " + r.name, image=img[i in self.checked],
                        values=["" if v is None else v for v in values], tags=tags, open=open_all)
            self.shown.append(i)
            for c in children.get(i, []):
                add(str(i), c)

        for i in children.get(None, []):
            add("", i)
        self._update_count()

    def _update_count(self) -> None:
        rows = self.scan.rows if self.scan else []
        if not rows:
            self.count.set("")
            return
        cats = sum(1 for i in self.matched if rows[i].level == "category")
        items = len(self.matched) - cats
        total_cats = sum(1 for r in rows if r.level == "category")
        self.count.set(f"Showing {cats} of {total_cats} categories and {items} of {len(rows) - total_cats} items"
                       f"   |   {len(self.checked)} ticked")

    def _open_all(self, on: bool) -> None:
        def walk(iid: str) -> None:
            for c in self.tree.get_children(iid):
                self.tree.item(c, open=on)
                walk(c)
        walk("")

    # ----------------------------------------------------------------- ticks
    def _on_click(self, e) -> Optional[str]:
        if self.tree.identify_region(e.x, e.y) != "tree":
            return None
        if "image" not in self.tree.identify_element(e.x, e.y):
            return None
        iid = self.tree.identify_row(e.y)
        if iid:
            self.toggle([iid])
            return "break"
        return None

    def _on_space(self, _e) -> str:
        self.toggle(list(self.tree.selection()))
        return "break"

    def toggle(self, iids: list[str]) -> None:
        """Tick or untick rows. A category takes the rows shown under it
        along: ticking 'Council' ticks the agendas listed under it."""
        if not iids:
            return
        on = int(iids[0]) not in self.checked
        todo: list[str] = []

        def walk(iid: str) -> None:
            todo.append(iid)
            for c in self.tree.get_children(iid):
                walk(c)
        for iid in iids:
            walk(iid)
        for iid in todo:
            (self.checked.add if on else self.checked.discard)(int(iid))
            self.tree.item(iid, image=self.app.box_images[on])
        if self.f_ticked.get() and not on:
            self.refresh()
        else:
            self._update_count()

    def tick_shown(self) -> None:
        self.checked |= set(self.shown)
        self.refresh()

    def untick_all(self) -> None:
        self.checked.clear()
        self.refresh()

    def ticked_rows(self) -> list[InventoryRow]:
        rows = self.scan.rows if self.scan else []
        return [rows[i] for i in sorted(self.checked)]

    # ---------------------------------------------------------------- export
    def export(self) -> None:
        if not self.scan or not self.scan.rows:
            messagebox.showinfo("AutoCentral", "Nothing to export: read this module first.")
            return
        default = default_out(self.app.site.get())
        default = default.with_name(default.stem + f"_{self.m.key}.xlsx")
        default.parent.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(initialdir=str(default.parent), initialfile=default.name,
                                            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        rows = [self.scan.rows[i] for i in self.shown]
        out = export_xlsx(Path(path), self.app.site.get(), [self.scan], rows)
        self.app.log(f"Saved {out}")


class OverviewTab:
    """Every module at a glance: on the site or not, counts, last read, notes."""

    COLS = [("module", "Module", 190), ("present", "On this site", 90), ("cats", "Categories", 85),
            ("items", "Items", 70), ("known", "Created info known", 130), ("read", "Last read", 130),
            ("secs", "Seconds", 65), ("problems", "Notes", 600)]

    def __init__(self, app: "App", nb: ttk.Notebook) -> None:
        self.app = app
        self.frame = ttk.Frame(nb)
        pad = {"padx": 4, "pady": 3}
        ttk.Label(self.frame, text="Double-click a module to open its tab. 'Read all modules' (top right) "
                                   "reads every module again.", foreground="#555").pack(fill="x", **pad)
        self.tree = ttk.Treeview(self.frame, columns=[k for k, _, _ in self.COLS], show="headings", height=11)
        for k, label, w in self.COLS:
            self.tree.heading(k, text=label)
            self.tree.column(k, width=w, stretch=k == "problems")
        self.tree.pack(fill="x", **pad)
        self.tree.bind("<Double-1>", self._open)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_notes())
        ttk.Label(self.frame, text="Notes for the selected module").pack(anchor="w", **pad)
        self.notes = tk.Text(self.frame, height=10, wrap="word", state="disabled")
        self.notes.pack(fill="both", expand=True, **pad)
        bar = ttk.Frame(self.frame)
        bar.pack(fill="x", **pad)
        self.total = tk.StringVar()
        ttk.Label(bar, textvariable=self.total).pack(side="left")
        ttk.Button(bar, text="Open inventories folder", command=app.open_folder).pack(side="right")
        ttk.Button(bar, text="Export everything…", command=app.export_all).pack(side="right", padx=4)

    def refresh(self) -> None:
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        rows_total = 0
        for m in MODULES:
            tab = self.app.tabs[m.key]
            ms = tab.scan
            busy = "Reading… " if tab.reading else ""
            if ms is None:
                vals = [m.label, "not read", "", "", "", "", "", busy]
            elif not ms.present:
                vals = [ms.label or m.label, "no", "", "", "", ms.read_at, "", busy]
            else:
                cats = sum(1 for r in ms.rows if r.level == "category")
                known = sum(1 for r in ms.rows if r.created_by)
                rows_total += len(ms.rows)
                vals = [ms.label or m.label, "yes", cats, len(ms.rows) - cats, f"{known} of {len(ms.rows)}",
                        ms.read_at, round(ms.seconds), busy + " | ".join(ms.problems)]
            self.tree.insert("", "end", iid=m.key, values=vals)
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        self.total.set(f"{rows_total} categories and items in all." if rows_total else "")
        self._show_notes()

    def _show_notes(self) -> None:
        sel = self.tree.selection()
        text = ""
        if sel:
            ms = self.app.tabs[sel[0]].scan
            if ms is None:
                text = "Not read yet."
            elif not ms.present:
                text = "This module isn't on this site (its admin page isn't there)."
            else:
                text = "\n".join("• " + p for p in ms.problems) or "No notes."
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("end", text)
        self.notes.configure(state="disabled")

    def _open(self, _e) -> None:
        sel = self.tree.selection()
        if sel:
            self.app.nb.select(self.app.tabs[sel[0]].frame)


# ====================================================================== app
class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("AutoCentral")
        root.geometry("1450x900")
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.worker = Worker(self.events)
        self.worker.start()
        self.settings = load_settings()
        self.busy = False
        self.loaded_host = ""
        self.run_windows: dict[int, object] = {}
        self._then: Optional[Callable[[], None]] = None     # runs once the current job is done
        self.box_images = {False: _box_image(False), True: _box_image(True)}
        self._build()
        self.load_saved()
        root.after(150, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._close)

    # ================================================================ layout
    def _build(self) -> None:
        pad = {"padx": 4, "pady": 3}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Site URL").pack(side="left")
        self.site = tk.StringVar(value=self.settings.get("site", "https://cp-training19.civicplus.com"))
        site_entry = ttk.Entry(top, textvariable=self.site, width=42)
        site_entry.pack(side="left", padx=4)
        site_entry.bind("<Return>", lambda _e: self.load_saved())
        site_entry.bind("<FocusOut>", lambda _e: self.load_saved())
        self.btn_login = ttk.Button(top, text="Open Chrome and log in", command=self.open_chrome)
        self.btn_login.pack(side="left")
        self.status = tk.StringVar(value="Not connected.")
        ttk.Label(top, textvariable=self.status, foreground="#555").pack(side="left", padx=10)

        self.btn_stop = ttk.Button(top, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="right")
        self.btn_all = ttk.Button(top, text="Read all modules", command=lambda: self.read([m.key for m in MODULES]))
        self.btn_all.pack(side="right", padx=4)
        self.refresh_history = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Re-read the whole History Log", variable=self.refresh_history).pack(
            side="right", padx=6)
        self.with_history = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Created info (History Log)", variable=self.with_history).pack(side="right")

        # The log is packed first, at the bottom, at a fixed height; the tabs
        # take all the room that's left.
        logf = ttk.LabelFrame(self.root, text="Log")
        logf.pack(side="bottom", fill="x", **pad)
        ys = ttk.Scrollbar(logf, orient="vertical")
        self.logbox = tk.Text(logf, height=7, wrap="word", state="disabled", yscrollcommand=ys.set)
        ys.configure(command=self.logbox.yview)
        ys.pack(side="right", fill="y")
        self.logbox.pack(side="left", fill="both", expand=True)
        self.logbox.tag_configure("warn", foreground="#a15c00")
        self.logbox.tag_configure("error", foreground="#b00020")

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, **pad)
        self.tabs: dict[str, ModuleTab] = {}
        self.overview = OverviewTab(self, self.nb)
        self.nb.add(self.overview.frame, text="Overview")
        for m in MODULES:
            t = ModuleTab(self, self.nb, m)
            self.tabs[m.key] = t
            self.nb.add(t.frame, text=m.label)
        from .gui_agenda import AgendaActions
        self.agenda_actions = AgendaActions(self, self.tabs["agenda"])

    # ============================================================ site data
    def host(self) -> str:
        return host_of(self.site.get())

    def load_saved(self, force: bool = False) -> None:
        """Show what was last read on this site (if anything)."""
        host = self.host()
        if (self.busy and not force) or not host or host == self.loaded_host:
            return
        self.loaded_host = host
        saved = load_all(cache_dir(host), [m.key for m in MODULES])
        for m in MODULES:
            self.set_module(saved.get(m.key), m.key)
        self.overview.refresh()
        if saved:
            self.log(f"Showing the last read of {len(saved)} module(s) on {host}. "
                     "Use 'Read this module' or 'Read all modules' to read them again.")

    def set_module(self, ms: Optional[ModuleScan], key: str) -> None:
        tab = self.tabs[key]
        tab.set_scan(ms)
        label = ms.label if ms and ms.present and ms.label else tab.m.label
        if ms is not None and not ms.present:
            label += " (not on site)"
        self.nb.tab(tab.frame, text=label)

    # ================================================================ events
    def _poll(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self.log(ev[2], ev[1])
                elif kind == "status":
                    self.status.set(ev[1])
                elif kind == "loggedin":
                    self.site.set(ev[1])
                    self.load_saved(force=True)
                elif kind == "module":
                    self._module_done(ev[1], ev[2])
                elif kind == "done":
                    self.log(ev[1])
                elif kind == "result":
                    win = self.run_windows.get(ev[1])
                    if win is not None:
                        try:
                            win.add_result(ev[2])
                        except tk.TclError:        # its window was closed
                            pass
                elif kind == "job_done":
                    win = self.run_windows.pop(ev[1], None)
                    if win is not None:
                        try:
                            win.finished(ev[3])
                        except tk.TclError:
                            pass
                    if ev[2]:                      # a live run changed the site: read it again
                        self._then = lambda: self.read(["agenda"])
                elif kind == "idle":
                    self._set_busy(False)
                    for t in self.tabs.values():
                        if t.reading:
                            t.reading = False
                            t._update_info()
                    self.overview.refresh()
                    then, self._then = self._then, None
                    if then is not None:
                        then()
        except queue.Empty:
            pass
        self.root.after(150, self._poll)

    def _module_done(self, ms: ModuleScan, host: str) -> None:
        ms.read_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            save_scan(cache_dir(host), ms)
        except OSError as e:
            self.log(f"Couldn't save the read of {ms.label}: {e}", "warn")
        if host == self.host():
            self.set_module(ms, ms.module)
        self.overview.refresh()

    def log(self, msg: str, level: str = "info") -> None:
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{datetime.now():%H:%M:%S}  {msg}\n", level)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in [self.btn_all, self.btn_login] + [t.btn_read for t in self.tabs.values()] + \
                self.agenda_actions.buttons:
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _submit(self, job: Callable[[], None]) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.worker.jobs.put(job)

    # ============================================================== actions
    def open_chrome(self) -> None:
        site = self.site.get().strip()
        if not site:
            messagebox.showwarning("AutoCentral", "Enter the site URL first.")
            return
        self.settings["site"] = site
        save_settings(self.settings)
        attach = os.environ.get("AUTOCENTRAL_ATTACH") or None
        ev = self.events

        def job() -> None:
            w = self.worker
            if w.browser is not None and not attach:
                try:
                    w.browser.quit()
                except Exception:
                    pass
            b = Browser(site, w.reporter(), w.cancel)
            b.start(attach)
            w.browser = b
            if not b.is_logged_in():
                b.open_for_login()
                ev.put(("status", "Waiting for you to log in (including 2FA)…"))
                ev.put(("log", "info", "Log in in the Chrome window. The tool takes over once you're in."))
                b.wait_for_login()
            ev.put(("status", f"Logged in to {b.host}."))
            ev.put(("loggedin", b.site_url))
            ev.put(("log", "info", f"Logged in to {b.host}. Open a module's tab and click 'Read this module', "
                                   "or click 'Read all modules'."))
        self._submit(job)

    def save(self) -> None:
        save_settings(self.settings)

    def run_job(self, win, live: bool) -> Optional[Path]:
        """Run an Agenda Center job (dry run or live) on the worker. Results go
        to the run window and to runs/<site>/<time>_dryrun|live/results.csv."""
        from .gui_agenda import RUNS
        from .agendacenter.jobs import AgendaJobs
        b = self.worker.browser
        if b is None or self.busy:
            win.state.set("Busy or not logged in: wait for the current read or run to finish, then try again.")
            return None
        run_dir = new_run_dir(RUNS / b.host, live)
        ev = self.events
        wid = id(win)
        self.run_windows[wid] = win
        self.log(("LIVE RUN: " if live else "Dry run: ") + win.what)

        def job() -> None:
            rep = Reporter(on_log=lambda level, msg: ev.put(("log", level, msg)),
                           on_result=lambda r: ev.put(("result", wid, r)), run_dir=run_dir)
            ok = False
            try:
                win.job(AgendaJobs(self.worker.browser, rep, live))
                ok = True
            finally:
                ev.put(("job_done", wid, live, ok))
        self._submit(job)
        return run_dir

    def read(self, keys: list[str], then: Optional[Callable[[], None]] = None) -> None:
        b = self.worker.browser
        if b is None:
            messagebox.showinfo("AutoCentral", "Click 'Open Chrome and log in' first.")
            return
        if self.busy:
            return
        if self.host() != b.host:
            messagebox.showinfo("AutoCentral", f"Chrome is logged in to {b.host}, but the Site URL says "
                                               f"{self.host()}. Click 'Open Chrome and log in' for the new site.")
            return
        for k in keys:
            self.tabs[k].set_reading()
        self.overview.refresh()
        history = self.with_history.get()
        refresh = self.refresh_history.get()
        ev = self.events
        host = b.host

        def job() -> None:
            w = self.worker
            w.browser.r = w.reporter()
            t0 = time.monotonic()
            sc = Scanner(w.browser, on_module_done=lambda ms: ev.put(("module", ms, host)),
                         cache_dir=cache_dir(host), refresh_history=refresh)
            sc.scan(keys, with_history=history)
            ev.put(("done", f"Read {len(keys)} module(s) in {time.monotonic() - t0:.0f}s."))
        self._submit(job)
        self._then = then

    def stop(self) -> None:
        self.worker.cancel.cancel()
        self.log("Stopping after the current page…", "warn")

    def export_all(self) -> None:
        scans = [t.scan for t in self.tabs.values() if t.scan is not None]
        if not scans:
            messagebox.showinfo("AutoCentral", "Nothing read yet.")
            return
        default = default_out(self.site.get())
        default.parent.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(initialdir=str(default.parent), initialfile=default.name,
                                            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        out = export_xlsx(Path(path), self.site.get(), scans)
        self.log(f"Saved {out}")

    def open_folder(self) -> None:
        folder = default_out(self.site.get()).parent
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def _close(self) -> None:
        if self.busy and not messagebox.askyesno("AutoCentral", "Something is still being read. Stop it and close?"):
            return
        self.worker.cancel.cancel()
        b = self.worker.browser
        if b is not None and not os.environ.get("AUTOCENTRAL_ATTACH"):
            try:
                b.quit()
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
