"""Finish agendas several categories at a time: one Chrome tab per category,
all in the same logged-in Chrome, each driven by its own WebDriver session
so the tabs really work at the same time.

In each tab, agenda after agenda: Save and Publish in one request sent
straight to the site (builds an item-based agenda's HTML page, sets its
video links, Notify Me: Don't Send), then its minutes (Save and Publish,
Don't Send). Exactly the requests the one-tab way sends, just several
categories at once. Anything that isn't a plain draft is handed back to be
done the usual way.
"""

from __future__ import annotations

import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# Measured on training19 (2026-11-19): 6 tabs at once made each request 7-10x slower
# (about 5 agendas a minute in all, against about 10 one at a time), and two tabs gave up after
# a single request took over 2 minutes. Central seems to work through them one after another.
# So one at a time is the default; more tabs only for trying again later.
DEFAULT_TABS = 1


@dataclass
class Job:
    agenda_id: int
    video: Optional[str] = None
    additional: Optional[str] = None
    save: bool = True                  # save (and publish) the agenda
    minutes: Optional[Path] = None     # then add these minutes
    ref: object = None                 # the caller's own handle (the task)
    results: list = field(default_factory=list)   # [(action, "ok" | problem)]
    handed_back: bool = False          # not a plain draft: do it the usual way


def finish(site, groups: dict, tabs: int = DEFAULT_TABS, log: Callable[[str], None] = lambda m: None,
           cancelled: Callable[[], bool] = lambda: False) -> list[Job]:
    """groups: {category id: (Category, [Job])}. Returns every Job with its results."""
    from selenium import webdriver
    addr = (site.b.d.capabilities.get("goog:chromeOptions") or {}).get("debuggerAddress")
    if not addr:
        raise RuntimeError("Can't find the Chrome debugging address to open more tabs.")
    base = site.b.site_url
    save_js, minutes_js = site._DIRECT_SAVE_JS, site._DIRECT_MINUTES_JS
    lock = threading.Lock()

    def one_category(cat, jobs: list[Job], part: str = "") -> list[Job]:
        opts = webdriver.ChromeOptions()
        opts.add_experimental_option("debuggerAddress", addr)
        drv = webdriver.Chrome(options=opts)
        try:
            drv.switch_to.new_window("tab")
            drv.get(f"{base}/Admin/AgendaCenter/Category/Detail/{cat.id}")
            drv.set_script_timeout(600)
            done = 0
            for j in jobs:
                if cancelled():
                    break
                if j.save:
                    r = drv.execute_async_script(save_js, j.agenda_id, cat.id, j.video, j.additional, True, False) or {}
                    if r.get("published"):
                        j.handed_back = True
                        continue
                    j.results.append(("save", "ok" if r.get("ok") else (r.get("error") or "no answer")))
                    if not r.get("ok"):
                        continue
                if j.minutes is not None:
                    data = base64.b64encode(Path(j.minutes).read_bytes()).decode()
                    r = drv.execute_async_script(minutes_js, j.agenda_id, cat.id, Path(j.minutes).name, data) or {}
                    if r.get("cloud"):
                        j.handed_back = True
                        continue
                    j.results.append(("minutes", "ok" if r.get("ok") else (r.get("error") or "no answer")))
                done += 1
                if done % 10 == 0:
                    with lock:
                        log(f"{cat.name}{part}: {done} of {len(jobs)} agenda(s) finished")
            with lock:
                log(f"{cat.name}{part}: {done} of {len(jobs)} agenda(s) finished in its own tab")
            return jobs
        finally:
            try:
                drv.close()                 # this tab only
            except Exception:
                pass
            try:
                drv.service.stop()          # this session's driver; Chrome and the other tabs stay
            except Exception:
                pass

    # Share the work evenly: a big category is spread over several tabs and
    # small ones get a tab each, so no tab sits idle while one big category
    # is still going.
    total = sum(len(v) for _, v in groups.values())
    size = max(1, -(-total // max(1, tabs)))
    shards = []
    for cat, jobs in groups.values():
        n = -(-len(jobs) // size) if jobs else 0
        for k in range(n):
            chunk = jobs[k::n]                      # interleaved, so each part has a mix of dates
            shards.append((cat, chunk, f" (part {k + 1} of {n})" if n > 1 else ""))
    shards.sort(key=lambda s: -len(s[1]))           # biggest first
    out: list[Job] = []
    with ThreadPoolExecutor(max_workers=max(1, tabs)) as pool:
        futs = {pool.submit(one_category, cat, chunk, part): (cat, chunk, part) for cat, chunk, part in shards}
        for fut in as_completed(futs):
            cat, chunk, part = futs[fut]
            try:
                out += fut.result()
            except Exception as e:
                with lock:
                    log(f"{cat.name}{part}: its tab stopped ({type(e).__name__}: {e}); the rest is done the usual way")
                for j in chunk:
                    if not j.results:
                        j.handed_back = True
                out += chunk
    return out
