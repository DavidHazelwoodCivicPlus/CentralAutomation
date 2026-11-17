"""The window (Tkinter). One background thread owns the browser; the window
talks to it through queues, so the window never freezes during a scan."""

from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from .browser import Browser
from .cli import ROOT, cache_dir, default_out
from .inventory import RowFilter, export_xlsx, select
from .models import COLUMNS, InventoryRow, ModuleScan
from .modules import MODULES
from .reporting import CancelToken, Cancelled, Reporter
from .scanner import Scanner

SETTINGS = ROOT / "settings.json"
ANY = "(any)"


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


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("AutoCentral: site inventory")
        root.geometry("1400x850")
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.worker = Worker(self.events)
        self.worker.start()
        self.settings = load_settings()
        self.scans: list[ModuleScan] = []
        self.rows: list[InventoryRow] = []
        self.shown: list[InventoryRow] = []
        self.sort_key: Optional[str] = None
        self.sort_desc = False
        self.busy = False
        self._build()
        root.after(150, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._close)

    # ================================================================ layout
    def _build(self) -> None:
        pad = {"padx": 4, "pady": 3}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Site URL").pack(side="left")
        self.site = tk.StringVar(value=self.settings.get("site", "https://cp-training19.civicplus.com"))
        ttk.Entry(top, textvariable=self.site, width=45).pack(side="left", padx=4)
        self.btn_login = ttk.Button(top, text="Open Chrome and log in", command=self.open_chrome)
        self.btn_login.pack(side="left")
        self.status = tk.StringVar(value="Not connected.")
        ttk.Label(top, textvariable=self.status, foreground="#555").pack(side="left", padx=10)

        mods = ttk.LabelFrame(self.root, text="Modules to read")
        mods.pack(fill="x", **pad)
        self.mod_vars: dict[str, tk.BooleanVar] = {}
        for i, m in enumerate(MODULES):
            v = tk.BooleanVar(value=True)
            self.mod_vars[m.key] = v
            ttk.Checkbutton(mods, text=m.label, variable=v).grid(row=i // 5, column=i % 5, sticky="w", padx=6)
        side = ttk.Frame(mods)
        side.grid(row=0, column=5, rowspan=2, sticky="e", padx=10)
        ttk.Button(side, text="All", width=6, command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(side, text="None", width=6, command=lambda: self._set_all(False)).pack(side="left", padx=2)
        self.with_history = tk.BooleanVar(value=True)
        ttk.Checkbutton(side, text="Who created / when (History Log)", variable=self.with_history).pack(
            side="left", padx=8)
        self.refresh_history = tk.BooleanVar(value=False)
        ttk.Checkbutton(side, text="Re-read the whole History Log", variable=self.refresh_history).pack(
            side="left", padx=4)
        self.btn_scan = ttk.Button(side, text="Scan (read-only)", command=self.scan)
        self.btn_scan.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(side, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")

        flt = ttk.LabelFrame(self.root, text="Filter the table")
        flt.pack(fill="x", **pad)
        self.f_module = self._combo(flt, "Module", 0)
        self.f_level = self._combo(flt, "Level", 2, ["category", "item"])
        self.f_status = self._combo(flt, "Status", 4)
        self.f_by = self._combo(flt, "Created by", 6, width=24)
        ttk.Label(flt, text="Created from").grid(row=0, column=8, padx=(10, 2))
        self.f_from = tk.StringVar()
        ttk.Entry(flt, textvariable=self.f_from, width=17).grid(row=0, column=9)
        ttk.Label(flt, text="to").grid(row=0, column=10, padx=2)
        self.f_to = tk.StringVar()
        ttk.Entry(flt, textvariable=self.f_to, width=17).grid(row=0, column=11)
        ttk.Label(flt, text="Search").grid(row=1, column=0, sticky="e", pady=3)
        self.f_text = tk.StringVar()
        ttk.Entry(flt, textvariable=self.f_text, width=40).grid(row=1, column=1, columnspan=3, sticky="w")
        self.f_regex = tk.BooleanVar()
        ttk.Checkbutton(flt, text="regex", variable=self.f_regex, command=self.refresh).grid(row=1, column=4)
        ttk.Label(flt, text="dates: YYYY-MM-DD or YYYY-MM-DD HH:MM", foreground="#777").grid(
            row=1, column=8, columnspan=4, sticky="w", padx=(10, 0))
        ttk.Button(flt, text="Clear filters", command=self.clear_filters).grid(row=1, column=6, sticky="w")
        for v in (self.f_from, self.f_to, self.f_text):
            v.trace_add("write", lambda *_: self.refresh())

        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True, **pad)
        cols = [k for k, _ in COLUMNS]
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        widths = {"module": 140, "level": 70, "category": 200, "name": 300, "status": 90, "created_on": 140,
                  "created_by": 140, "modified_on": 110, "modified_by": 130, "detail": 260, "ids": 140,
                  "category_id": 80, "item_id": 70}
        for k, label in COLUMNS:
            self.tree.heading(k, text=label, command=lambda k=k: self.sort_by(k))
            self.tree.column(k, width=widths.get(k, 100), stretch=k in ("name", "detail"))
        ys = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.tree.tag_configure("category", background="#eef2f7")

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", **pad)
        self.count = tk.StringVar(value="No scan yet.")
        ttk.Label(bar, textvariable=self.count).pack(side="left")
        ttk.Button(bar, text="Export shown rows…", command=lambda: self.export(True)).pack(side="right")
        ttk.Button(bar, text="Export everything…", command=lambda: self.export(False)).pack(side="right", padx=4)
        ttk.Button(bar, text="Open inventories folder", command=self.open_folder).pack(side="right")

        logf = ttk.LabelFrame(self.root, text="Log")
        logf.pack(fill="x", **pad)
        self.log = tk.Text(logf, height=9, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("warn", foreground="#a15c00")
        self.log.tag_configure("error", foreground="#b00020")

    def _combo(self, parent, label: str, col: int, values: Optional[list[str]] = None, width: int = 16):
        ttk.Label(parent, text=label).grid(row=0, column=col, padx=(10 if col else 2, 2), sticky="e")
        var = tk.StringVar(value=ANY)
        cb = ttk.Combobox(parent, textvariable=var, values=[ANY] + (values or []), width=width, state="readonly")
        cb.grid(row=0, column=col + 1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        var.combo = cb
        return var

    def _set_all(self, on: bool) -> None:
        for v in self.mod_vars.values():
            v.set(on)

    # ================================================================ events
    def _poll(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._log(ev[2], ev[1])
                elif kind == "status":
                    self.status.set(ev[1])
                elif kind == "module":
                    self._add_scan(ev[1])
                elif kind == "done":
                    self._log(ev[1])
                elif kind == "idle":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.root.after(150, self._poll)

    def _log(self, msg: str, level: str = "info") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", level)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.btn_scan.configure(state="disabled" if busy else "normal")
        self.btn_login.configure(state="disabled" if busy else "normal")
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
            if w.browser is not None:
                w.browser.quit() if not attach else None
            b = Browser(site, w.reporter(), w.cancel)
            b.start(attach)
            w.browser = b
            if not b.is_logged_in():
                b.open_for_login()
                ev.put(("status", "Waiting for you to log in (including 2FA)…"))
                ev.put(("log", "info", "Log in in the Chrome window. The tool takes over once you're in."))
                b.wait_for_login()
            ev.put(("status", f"Logged in to {b.host}."))
            ev.put(("log", "info", f"Logged in to {b.host}. Pick modules and click Scan."))
        self._submit(job)

    def scan(self) -> None:
        if self.worker.browser is None:
            messagebox.showinfo("AutoCentral", "Click 'Open Chrome and log in' first.")
            return
        keys = [k for k, v in self.mod_vars.items() if v.get()]
        if not keys:
            messagebox.showinfo("AutoCentral", "Tick at least one module.")
            return
        self.scans, self.rows = [], []
        self.refresh()
        history = self.with_history.get()
        refresh = self.refresh_history.get()
        ev = self.events

        def job() -> None:
            import time
            w = self.worker
            w.browser.r = w.reporter()
            t0 = time.monotonic()
            sc = Scanner(w.browser, on_module_done=lambda ms: ev.put(("module", ms)),
                         cache_dir=cache_dir(w.browser.host), refresh_history=refresh)
            sc.scan(keys, with_history=history)
            ev.put(("done", f"Scan finished in {time.monotonic() - t0:.0f}s."))
        self._submit(job)

    def stop(self) -> None:
        self.worker.cancel.cancel()
        self._log("Stopping after the current page…", "warn")

    def _add_scan(self, ms: ModuleScan) -> None:
        self.scans.append(ms)
        self.rows.extend(ms.rows)
        self._update_choices()
        self.refresh()

    def _update_choices(self) -> None:
        mods = sorted({r.module_label or r.module for r in self.rows})
        statuses = sorted({r.status for r in self.rows if r.status})
        people = sorted({r.created_by for r in self.rows if r.created_by}) + ["unknown"]
        for var, vals in ((self.f_module, mods), (self.f_status, statuses), (self.f_by, people)):
            var.combo.configure(values=[ANY] + vals)

    def current_filter(self) -> RowFilter:
        pick = lambda v: [] if v.get() in ("", ANY) else [v.get()]
        return RowFilter(modules=pick(self.f_module), levels=pick(self.f_level), statuses=pick(self.f_status),
                         created_by=pick(self.f_by), created_from=self.f_from.get().strip(),
                         created_to=self.f_to.get().strip(), text=self.f_text.get().strip(),
                         regex=self.f_regex.get())

    def clear_filters(self) -> None:
        for v in (self.f_module, self.f_level, self.f_status, self.f_by):
            v.set(ANY)
        for v in (self.f_from, self.f_to, self.f_text):
            v.set("")
        self.f_regex.set(False)
        self.refresh()

    def sort_by(self, key: str) -> None:
        self.sort_desc = not self.sort_desc if self.sort_key == key else False
        self.sort_key = key
        self.refresh()

    def refresh(self) -> None:
        self.shown = select(self.rows, self.current_filter())
        if self.sort_key:
            k = self.sort_key
            num = k in ("category_id", "item_id")
            self.shown.sort(key=lambda r: (getattr(r, k) is None, getattr(r, k) or (0 if num else "")),
                            reverse=self.sort_desc)
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.shown):
            d = r.as_dict()
            self.tree.insert("", "end", iid=str(i), tags=(r.level,),
                             values=[("" if d.get(k) is None else d.get(k)) for k, _ in COLUMNS])
        total = len(self.rows)
        cats = sum(1 for r in self.shown if r.level == "category")
        self.count.set(f"Showing {len(self.shown)} of {total} rows ({cats} categories, "
                       f"{len(self.shown) - cats} items)." if total else "No scan yet.")

    def export(self, shown_only: bool) -> None:
        if not self.scans:
            messagebox.showinfo("AutoCentral", "Scan first.")
            return
        default = default_out(self.site.get())
        path = filedialog.asksaveasfilename(initialdir=str(default.parent), initialfile=default.name,
                                            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        default.parent.mkdir(parents=True, exist_ok=True)
        out = export_xlsx(Path(path), self.site.get(), self.scans, self.shown if shown_only else None)
        self._log(f"Saved {out}")

    def open_folder(self) -> None:
        folder = default_out(self.site.get()).parent
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def _close(self) -> None:
        if self.busy and not messagebox.askyesno("AutoCentral", "A scan is running. Stop it and close?"):
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
