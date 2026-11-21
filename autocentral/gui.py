"""The window (Tkinter): an Overview tab plus one tab per module.

Each module tab shows that module's categories and items as a tree, with its
own filters, its own columns and a tick box on every row. Its actions bar
(gui_agenda.py, gui_actions.py) publishes, unpublishes and deletes the ticked rows.

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
from .modules import FIXED_LABELS, MODULES, SLOW_KEYS, VIEW_ONLY_KEYS, ModuleDef
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
# Wider columns for module fields that hold addresses or lists (the rest get 120).
FIELD_WIDTHS = {"URL": 280, "Parent": 220, "Widgets": 240, "Kind": 150, "Links to": 220, "Shown as": 200,
                "File name": 200, "Pages": 300, "In Document Center": 220, "Works": 160, "Folder": 220}
# Fields shown in the preview panel, not as columns (still searched and exported).
HIDDEN_FIELDS = {"Text", "HTML"}
# Row colours by status (pages and their links; the rest keep the default).
STATUS_COLOURS = {"draft": "#a15c00", "unpublished": "#a15c00", "not found": "#b00020", "broken": "#b00020",
                  "external": "#1f5fa8", "linked page": "#6a3d9a"}


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


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def file_type(r: InventoryRow) -> str:
    """A file's or image's type (pdf, png ...), for the File types tick boxes."""
    t = (r.fields.get("Type") or "").strip().lower().lstrip(".")
    if not t and r.status in ("file", "image file", "image"):
        name = r.fields.get("File name") or r.fields.get("URL") or ""
        m = re.search(r"\.([A-Za-z0-9]{2,5})$", name.split("?")[0])
        t = m.group(1).lower() if m else "(no extension)"
    return t


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
        return Reporter(on_log=lambda level, msg: self.events.put(("log", level, msg)),
                        on_progress=lambda done, total, label: self.events.put(("progress", done, total, label)))


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

        self.show_box = ttk.LabelFrame(f, text="Show")
        self.show_box.pack(fill="x", **pad)
        self.show_vars: dict[str, tk.BooleanVar] = {}
        self.type_box = ttk.LabelFrame(f, text="File types")
        self.type_vars: dict[str, tk.BooleanVar] = {}
        self._pad = pad

        self.preview = None
        if self.m.key == "pages":
            # the table on top, the text / HTML preview of the selected row underneath (drag to resize)
            panes = ttk.Panedwindow(f, orient="vertical")
            panes.pack(fill="both", expand=True, **pad)
            mid = ttk.Frame(panes)
            panes.add(mid, weight=3)
            self.preview = BlockPreview(self, panes)
            panes.add(self.preview.frame, weight=2)
        else:
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
        for st, colour in STATUS_COLOURS.items():
            self.tree.tag_configure("st_" + st.replace(" ", "_"), foreground=colour)
        self.tree.tag_configure("context", foreground="#999999")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Double-1>", self._on_double)
        if self.preview is not None:
            self.tree.bind("<<TreeviewSelect>>", lambda _e: self.preview.show_selected(), add="+")
            # ← and → in the table step through the pages shown (instead of folding the tree).
            self.tree.bind("<Left>", lambda _e: self.preview.step(-1) or "break")
            self.tree.bind("<Right>", lambda _e: self.preview.step(1) or "break")
        self.tree.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_name_width", self.tree.column("#0", "width")),
                       add="+")
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
        self.tree.configure(displaycolumns="#all")      # the old list may name columns that are going
        self.tree.configure(columns=cols)
        self.tree.heading("#0", text="Name", command=lambda: self.sort_by("name"))
        self.tree.column("#0", width=getattr(self, "_name_width", 380), stretch=False)
        specs = BASE_COLS + [(f"f{i}", label, FIELD_WIDTHS.get(label, 120)) for i, label in enumerate(fields)] + TAIL_COLS
        for k, label, width in specs:
            self.tree.heading(k, text=label, command=lambda k=k: self.sort_by(k))
            self.tree.column(k, width=width, stretch=False)

    # ------------------------------------------------------------------ data
    def set_scan(self, ms: Optional[ModuleScan], keep_ticks: bool = False) -> None:
        same = keep_ticks and ms is self.scan
        self.scan = ms
        if not same:
            self.checked.clear()
        self.reading = False
        rows = ms.rows if ms else []
        fields: list[str] = []
        for r in rows:                  # the module's own columns, skipping ones empty everywhere
            for k, v in r.fields.items():
                if v and k not in fields and k not in HIDDEN_FIELDS:
                    fields.append(k)
        self._set_columns(fields)
        # Hide the standard columns no row fills (Created ... when History Log wasn't read).
        shown = [k for k, _, _ in BASE_COLS if k == "status" or any(getattr(r, k, "") for r in rows)]
        notes = ["notes"] if any(self._notes(r) for r in rows) else []
        self.tree.configure(displaycolumns=shown + [f"f{i}" for i in range(len(fields))] + notes + ["id"])
        self.parent = self._parents(rows)
        statuses = sorted({r.status for r in rows if r.status})
        people = sorted({r.created_by for r in rows if r.created_by})
        self._fill_show_box(rows)
        self.cb_status.configure(values=[ANY] + statuses)
        self.cb_by.configure(values=[ANY, NO_INFO] + people)
        if self.f_status.get() not in [ANY] + statuses:
            self.f_status.set(ANY)
        if self.f_by.get() not in [ANY, NO_INFO] + people:
            self.f_by.set(ANY)
        self._update_info()
        self.refresh()

    def _fill_show_box(self, rows: list[InventoryRow]) -> None:
        """One tick box per status (for Pages: per page status and per kind of
        link, widget and image), with how many rows have it. Unticking hides them."""
        counts: dict[str, int] = {}
        for r in rows:
            if r.status:
                counts[r.status] = counts.get(r.status, 0) + 1
        old = {k: v.get() for k, v in self.show_vars.items()}
        for w in self.show_box.winfo_children():
            w.destroy()
        self.show_vars = {}
        if not counts:
            ttk.Label(self.show_box, text="(read the module first)", foreground="#777").pack(side="left", padx=4)
            return
        for st in sorted(counts):
            v = tk.BooleanVar(value=old.get(st, True))
            self.show_vars[st] = v
            ttk.Checkbutton(self.show_box, text=f"{st} ({counts[st]})", variable=v,
                            command=self.refresh).pack(side="left", padx=(4, 6))
        ttk.Button(self.show_box, text="All", width=4, command=lambda: self._show_all(True)).pack(side="right", padx=2)
        ttk.Button(self.show_box, text="None", width=5, command=lambda: self._show_all(False)).pack(side="right", padx=2)
        # File types (pdf, png ...) of files and images, when there are any.
        types: dict[str, int] = {}
        for r in rows:
            t = file_type(r)
            if t:
                types[t] = types.get(t, 0) + 1
        old_t = {k: v.get() for k, v in self.type_vars.items()}
        for w in self.type_box.winfo_children():
            w.destroy()
        self.type_vars = {}
        if not types:
            self.type_box.pack_forget()
            return
        self.type_box.pack(fill="x", after=self.show_box, **self._pad)
        for t in sorted(types, key=lambda x: (-types[x], x))[:30]:
            v = tk.BooleanVar(value=old_t.get(t, True))
            self.type_vars[t] = v
            ttk.Checkbutton(self.type_box, text=f"{t} ({types[t]})", variable=v, command=self.refresh).pack(
                side="left", padx=(4, 6))
        ttk.Button(self.type_box, text="All", width=4, command=lambda: self._types_all(True)).pack(side="right", padx=2)
        ttk.Button(self.type_box, text="None", width=5, command=lambda: self._types_all(False)).pack(side="right", padx=2)

    def _types_all(self, on: bool) -> None:
        for v in self.type_vars.values():
            v.set(on)
        self.refresh()

    def _show_all(self, on: bool) -> None:
        for v in self.show_vars.values():
            v.set(on)
        self.refresh()

    def _on_double(self, e) -> Optional[str]:
        """Double-click a row with an address: open it in the logged-in Chrome
        (a page opens with Live Edit on)."""
        iid = self.tree.identify_row(e.y)
        if not iid or not self.scan:
            return None
        r = self.scan.rows[int(iid)]
        url = r.fields.get("URL", "")
        if not url:
            return None
        self.app.open_in_chrome(url, live_edit=(r.level == "category" and self.m.key == "pages"))
        return "break"

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
        hidden = {st for st, v in self.show_vars.items() if not v.get()}
        hidden_types = {t for t, v in self.type_vars.items() if not v.get()}
        gone: set[int] = set()
        if hidden or hidden_types:
            for i, r in enumerate(rows):
                p = self.parent.get(i)
                if r.status in hidden or (r.level == "item" and p is not None and rows[p].status in hidden):
                    gone.add(i)
                elif hidden_types and file_type(r) in hidden_types:
                    gone.add(i)
        self.matched = {i for i, r in enumerate(rows)
                        if i not in gone and flt.match(r) and (not ticked_only or i in self.checked)}
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
            if r.status in STATUS_COLOURS and i in self.matched:
                tags.append("st_" + r.status.replace(" ", "_"))
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
        ttk.Button(bar, text="Wipe the whole site…", command=app.wipe_site).pack(side="right", padx=12)

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


class BlockPreview:
    """Under the Pages table: the selected text block's readable text and its
    HTML (for a page: all its text blocks), with Copy HTML and a preview in
    your browser styled like the site."""

    def __init__(self, tab: "ModuleTab", parent) -> None:
        self.tab = tab
        self.frame = ttk.Frame(parent)
        bar = ttk.Frame(self.frame)
        bar.pack(fill="x")
        self.what = tk.StringVar(value="Select a page or a text block to see its content.")
        ttk.Label(bar, textvariable=self.what).pack(side="left")
        ttk.Button(bar, text="Next page ▶", command=lambda: self.step(1)).pack(side="right")
        ttk.Button(bar, text="◀ Previous page", command=lambda: self.step(-1)).pack(side="right", padx=4)
        ttk.Button(bar, text="Preview shown pages in browser", command=self.preview_all).pack(side="right", padx=8)
        ttk.Button(bar, text="Preview in browser", command=self.preview_in_browser).pack(side="right")
        ttk.Button(bar, text="Copy HTML", command=self.copy_html).pack(side="right", padx=4)

        nb = ttk.Notebook(self.frame)
        nb.pack(fill="both", expand=True)
        self.text = self._box(nb, "Text")
        self.html = self._box(nb, "HTML")
        self.rows: list[InventoryRow] = []
        self.page: Optional[InventoryRow] = None

    @staticmethod
    def _box(nb, label: str) -> tk.Text:
        f = ttk.Frame(nb)
        nb.add(f, text=label)
        ys = ttk.Scrollbar(f, orient="vertical")
        t = tk.Text(f, wrap="word", height=8, state="disabled", yscrollcommand=ys.set,
                    font=("Consolas", 9) if label == "HTML" else ("Segoe UI", 9))
        ys.configure(command=t.yview)
        ys.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        return t

    @staticmethod
    def _set(box: tk.Text, value: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", value)
        box.configure(state="disabled")

    def show_selected(self) -> None:
        sel = self.tab.tree.selection()
        if not sel or not self.tab.scan:
            return
        rows = self.tab.scan.rows
        i = int(sel[0])
        r = rows[i]
        if r.level == "category":
            self.page = r
            self.rows = [x for x in rows if x.level == "item" and x.status == "text block" and x.category_id == r.category_id]
        elif r.status == "text block":
            self.page = next((x for x in rows if x.level == "category" and x.category_id == r.category_id), None)
            self.rows = [r]
        else:
            self.page, self.rows = None, []
            self.what.set(f"{r.status}: {r.fields.get('URL', '')}")
            self._set(self.text, "\n".join(f"{k}: {v}" for k, v in r.fields.items() if v))
            self._set(self.html, r.detail)
            return
        name = self.page.name if self.page else ""
        shared = self.rows[0].fields.get("Shared", "") if len(self.rows) == 1 else ""
        self.what.set(f"{name}: {len(self.rows)} text block(s)" + (f" (shared: {shared})" if shared else "")
                      if self.rows else f"{name}: no text blocks (read Pages again if this read is from before text blocks were kept)")
        sep = "\n\n" + "-" * 40 + "\n\n"
        self._set(self.text, sep.join(x.fields.get("Text", "") for x in self.rows))
        self._set(self.html, sep.join(x.fields.get("HTML", "") for x in self.rows))

    def step(self, d: int) -> None:
        """Select the previous / next page shown in the table and show it."""
        tab = self.tab
        if not tab.scan:
            return
        rows = tab.scan.rows
        pages = [i for i in tab.shown if rows[i].level == "category"]
        if not pages:
            return
        sel = tab.tree.selection()
        cur = int(sel[0]) if sel else None
        if cur is not None and rows[cur].level != "category":       # on a row under a page: start from that page
            cur = next((i for i in pages if rows[i].category_id == rows[cur].category_id), None)
        if cur in pages:
            k = pages.index(cur) + d
        else:
            k = 0 if d > 0 else len(pages) - 1
        k = max(0, min(len(pages) - 1, k))
        iid = str(pages[k])
        tab.tree.selection_set(iid)
        tab.tree.focus(iid)
        tab.tree.see(iid)
        self.show_selected()

    def preview_all(self) -> None:
        """Every page shown in the table, in one preview page in your browser:
        ← and → (or the buttons) step through them."""
        import webbrowser
        from .sitecontent import preview_pages_html
        tab = self.tab
        if not tab.scan:
            return
        rows = tab.scan.rows
        home = next((x for x in rows if x.level == "category" and x.category_id == 1), None)
        sheets = (home.extra.get("stylesheets") if home else None) or []
        pages = []
        for i in tab.shown:
            r = rows[i]
            if r.level != "category":
                continue
            blocks = [x.fields.get("HTML", "") for x in rows if x.level == "item" and x.status == "text block"
                      and x.category_id == r.category_id]
            pages.append({"title": r.name, "status": r.status, "path": r.fields.get("Parent", ""),
                          "url": r.fields.get("URL", ""), "blocks": blocks})
        if not pages:
            messagebox.showinfo("Preview", "No pages are shown in the table.")
            return
        out = ROOT / "cache" / "previews" / "shown_pages.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(preview_pages_html(self.tab.app.site.get(), pages, sheets), encoding="utf-8")
        webbrowser.open(out.as_uri())
        self.tab.app.log(f"Preview of {len(pages)} page(s) opened in your browser: ← and → step through them.")

    def copy_html(self) -> None:
        if not self.rows:
            return
        root = self.tab.frame.winfo_toplevel()
        root.clipboard_clear()
        root.clipboard_append("\n\n".join(x.fields.get("HTML", "") for x in self.rows))
        self.tab.app.log(f"Copied the HTML of {len(self.rows)} text block(s).")

    def preview_in_browser(self) -> None:
        """A page made from the text block(s) with the site's own stylesheets,
        opened in your browser."""
        import webbrowser
        from .sitecontent import preview_html
        if not self.rows:
            messagebox.showinfo("Preview", "Select a page or a text block first.")
            return
        scan = self.tab.scan
        home = next((x for x in scan.rows if x.level == "category" and x.category_id == 1), None) if scan else None
        sheets = (home.extra.get("stylesheets") if home else None) or []
        title = self.page.name if self.page else "Text block"
        out = ROOT / "cache" / "previews" / f"page_{self.page.category_id if self.page else 'block'}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(preview_html(self.tab.app.site.get(), title, [x.fields.get("HTML", "") for x in self.rows], sheets),
                       encoding="utf-8")
        webbrowser.open(out.as_uri())
        self.tab.app.log(f"Preview of {title} opened in your browser." +
                         ("" if sheets else " (Read Pages again to get the site's styles into previews.)"))


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
        self.working = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.working, foreground="#1f5fa8", width=46).pack(side="left", padx=6)
        self._busy_since = 0.0
        self._busy_what = ""
        self._eta_end: Optional[float] = None
        self._progress_start: dict[str, tuple[float, int]] = {}

        self.btn_stop = ttk.Button(top, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="right")
        self.btn_all = ttk.Button(top, text="Read all modules", command=lambda: self.read([m.key for m in MODULES if m.key not in SLOW_KEYS]))
        self.btn_all.pack(side="right", padx=4)
        self.refresh_history = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Re-read the whole History Log", variable=self.refresh_history).pack(
            side="right", padx=6)
        self.with_history = tk.BooleanVar(value=False)   # slow on busy modules: off unless ticked
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
        from .addons import add_tabs
        self.addons = add_tabs(self, self.nb)          # switchable project tabs (settings.json "addons")
        from .gui_plan import PlanTab, FindReplaceDialog, move_pages_dialog, move_staff_dialog
        self.plan = PlanTab(self, self.nb)
        self.nb.add(self.plan.frame, text="Plan")
        self.plan.refresh()
        from .gui_actions import ModuleActions
        from .gui_agenda import AgendaActions
        self.agenda_actions = AgendaActions(self, self.tabs["agenda"])
        self.module_actions = {k: ModuleActions(self, t) for k, t in self.tabs.items()
                               if k != "agenda" and k not in VIEW_ONLY_KEYS}
        self.last_first = tk.BooleanVar(value=self.settings.get("staff_last_first", True))
        ttk.Checkbutton(self.tabs["staff"].head, text="Last name, First name", variable=self.last_first,
                        command=self._staff_name_order).pack(side="right", padx=8)
        ttk.Button(self.tabs["staff"].head, text="Move ticked… (to the plan)",
                   command=lambda: move_staff_dialog(self)).pack(side="right", padx=4)
        pages = self.tabs["pages"]
        ttk.Button(pages.head, text="Find and replace…", command=lambda: FindReplaceDialog(self)).pack(side="right", padx=4)
        ttk.Button(pages.head, text="Move ticked pages… (to the plan)",
                   command=lambda: move_pages_dialog(self)).pack(side="right", padx=4)
        self.btn_links = ttk.Button(pages.head, text="Check links…", command=self.check_links)
        self.btn_links.pack(side="right", padx=4)
        ttk.Button(pages.head, text="Export site map…", command=self.export_sitemap).pack(side="right", padx=4)
        for key in ("pages", "docs"):
            ttk.Label(self.tabs[key].head, text="Double-click a row to open it in your browser.",
                      foreground="#777").pack(side="left", padx=12)

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
        if key in ("pages", "docs"):
            # Files and images on pages <-> Document Center / Public Images.
            from .sitecontent import cross_reference
            other = self.tabs["docs" if key == "pages" else "pages"]
            pages_ms, docs_ms = (ms, other.scan) if key == "pages" else (other.scan, ms)
            cross_reference(pages_ms, docs_ms)
            if other.scan is not None:
                other.set_scan(other.scan, keep_ticks=True)
        if ms is not None and key in FIXED_LABELS:
            ms.label = tab.m.label
        if key == "staff" and hasattr(self, "last_first"):
            from .sitecontent import set_staff_name_order
            set_staff_name_order(ms, self.last_first.get())
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
                elif kind == "progress":
                    self._progress(ev[1], ev[2], ev[3])
                elif kind == "loggedin":
                    self.site.set(ev[1])
                    self.load_saved(force=True)
                elif kind == "module":
                    self._module_done(ev[1], ev[2])
                elif kind == "updated":            # rows changed in place (link check): save and show
                    ms = self.tabs[ev[1]].scan
                    if ms is not None:
                        try:
                            save_scan(cache_dir(self.host()), ms)
                        except OSError as e:
                            self.log(f"Couldn't save {ms.label}: {e}", "warn")
                        self.tabs[ev[1]].set_scan(ms, keep_ticks=True)
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
                        key = getattr(win, "module", "agenda") if win is not None else "agenda"
                        if key:
                            self._then = lambda key=key: self.read([key])
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

    def _set_busy(self, busy: bool, what: str = "") -> None:
        if busy and not self.busy:
            self._busy_since = time.monotonic()
            self._busy_what = what or self._busy_what or "Working"
            self._progress_start = {}
            self.root.after(500, self._tick)
        if not busy:
            self.working.set("")
            self._busy_what, self._eta_end = "", None
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in [self.btn_all, self.btn_login, self.btn_links] + [t.btn_read for t in self.tabs.values()] + \
                self.agenda_actions.buttons + [b for a in self.module_actions.values() for b in a.buttons] + \
                self.plan.buttons:
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _tick(self, n: int = 1) -> None:
        """While busy: '.', '..', '...' twice a second, with time left (or,
        when there's no way to guess yet, time so far)."""
        if not self.busy:
            self.working.set("")
            return
        dots = "." * (n % 3 + 1)
        now = time.monotonic()
        if self._eta_end is not None:
            left = self._eta_end - now
            when = f"about {_mmss(left)} left" if left > 1 else "almost done"
        else:
            when = f"{_mmss(now - self._busy_since)} so far"
        self.working.set(f"{self._busy_what}{dots:<3}  {when}")
        self.root.after(500, lambda: self._tick(n + 1))

    def _progress(self, done: int, total: int, label: str) -> None:
        """A part of the job with a count (pages read, folders listed ...):
        time left from how fast it has gone so far."""
        now = time.monotonic()
        if label not in self._progress_start or done < self._progress_start[label][1]:
            self._progress_start[label] = (now, done)
            return
        t0, d0 = self._progress_start[label]
        if done > d0 and total > done:
            rate = (now - t0) / (done - d0)
            later = getattr(self, "_eta_after", 0.0)
            self._eta_end = now + rate * (total - done) + later
        elif done >= total:
            self._progress_start.pop(label, None)

    def _submit(self, job: Callable[[], None], what: str = "Working") -> None:
        if self.busy:
            return
        self._set_busy(True, what)
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
        """Run a module's job (dry run or live) on the worker. Results go to
        the run window and to runs/<site>/<time>_dryrun|live/results.csv."""
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
                factory = getattr(win, "factory", None) or AgendaJobs
                win.job(factory(self.worker.browser, rep, live))
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
        # First guess at the time: how long the same modules took last time.
        before = [self.tabs[k].scan.seconds for k in keys if self.tabs[k].scan and self.tabs[k].scan.seconds]
        names = ", ".join(self.tabs[k].m.label for k in keys[:2]) + (" …" if len(keys) > 2 else "")
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
        self._submit(job, f"Reading {names}")
        self._eta_end = time.monotonic() + sum(before) if len(before) == len(keys) else None
        self._then = then

    def wipe_site(self) -> None:
        """Every module's content, every page (not Home or the layout pages) and
        every document and image: dry run first, then the address typed."""
        if self.worker.browser is None:
            messagebox.showinfo("Wipe the whole site", "Open Chrome and log in first.")
            return
        host = self.worker.browser.host
        if not messagebox.askyesno(
                "Wipe the whole site",
                f"This deletes EVERYTHING on {host}:\n\n"
                "  every agenda, bid, FAQ, news flash, archive item, calendar and event, form, facility,\n"
                "  Resource Directory entry and staff department and person;\n"
                "  every page except Home and the layout pages;\n"
                "  every folder, document and image in Document Center and Public Images.\n\n"
                "It can't be undone. A dry run comes first and shows everything it would delete; the live "
                "run then needs the site's address typed.\n\nStart the dry run?", icon="warning"):
            return
        from .actions.wipe import SiteWipe
        from .gui_agenda import RunWindow
        RunWindow(self, "Wipe the whole site", f"Wipe EVERYTHING on {host}: modules, pages, documents and images "
                  "(Home and the layout pages stay).", lambda jobs: jobs.run(), factory=SiteWipe, module=None)

    def stop(self) -> None:
        self.worker.cancel.cancel()
        self.log("Stopping: ending the pages and requests in progress…", "warn")

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

    def _staff_name_order(self) -> None:
        from .sitecontent import set_staff_name_order
        self.settings["staff_last_first"] = self.last_first.get()
        save_settings(self.settings)
        tab = self.tabs["staff"]
        set_staff_name_order(tab.scan, self.last_first.get())
        tab.refresh()

    def open_in_chrome(self, url: str, live_edit: bool = False) -> None:
        """Open an address in the computer's own browser, not the tool's
        Chrome: the tool's Chrome may be busy reading, and files open there as
        odd downloads. Drafts show when you're logged in to the site there
        with Live Edit on."""
        import webbrowser
        site = self.site.get().strip().rstrip("/")
        full = url if re.match(r"^https?://", url) else site + ("" if url.startswith("/") else "/") + url
        webbrowser.open(full)
        self.log(f"Opened {full} in your browser.")

    def check_links(self) -> None:
        """Check every distinct link, file and image address on the read pages."""
        tab = self.tabs["pages"]
        b = self.worker.browser
        if b is None:
            messagebox.showinfo("AutoCentral", "Click 'Open Chrome and log in' first.")
            return
        if tab.scan is None or not tab.scan.rows:
            messagebox.showinfo("AutoCentral", "Read Pages first.")
            return
        if self.busy:
            return
        ev = self.events
        rows = tab.scan.rows

        def job() -> None:
            from .actions.engine import Relay
            from .linkcheck import check_rows
            w = self.worker
            w.browser.r = w.reporter()
            t0 = time.monotonic()
            check_rows(rows, w.browser.host, Relay(w.browser), lambda m: ev.put(("log", "info", m)),
                       cancel_check=w.cancel.check,
                       progress=lambda d, t, label: ev.put(("progress", d, t, label)))
            ev.put(("updated", "pages"))
            ev.put(("done", f"Link check took {time.monotonic() - t0:.0f}s."))
        self._submit(job, "Checking links")

    def export_sitemap(self) -> None:
        """The shown pages as a CivicPlus-style site map, one tier per column."""
        from .sitecontent import export_sitemap, tree_depths
        tab = self.tabs["pages"]
        if not tab.scan or not tab.scan.rows:
            messagebox.showinfo("AutoCentral", "Read Pages first.")
            return
        default = default_out(self.site.get())
        default = default.with_name(default.stem + "_sitemap.xlsx")
        default.parent.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(initialdir=str(default.parent), initialfile=default.name,
                                            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        rows = tab.scan.rows
        depths = tree_depths(rows, tab.parent)
        shown = [rows[i] for i in tab.shown if rows[i].level == "category"]
        out = export_sitemap(Path(path), self.site.get(), shown, depths)
        self.log(f"Saved the site map ({len(shown)} pages): {out}")

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
