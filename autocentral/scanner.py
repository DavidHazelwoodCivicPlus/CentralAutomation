"""Read-only inventory of every module: categories and items, plus who created
each one and when (from History Log). Nothing here clicks, saves or opens an
edit page."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import parse
from .browser import Browser, SiteError
from .historylog import HistoryLog, join_history
from .models import InventoryRow, ModuleScan
from .modules import MODULES, ModuleDef

# JavaScript step for Browser.frames(): on a classic module page, pick
# "View List" in the dropdown of one category. Picking a list view only
# navigates; it never changes anything.
VIEW_LIST_JS = r"""
const id = %d, argIndex = %d;
const sels = [...doc.querySelectorAll('select')].filter(s => {
    const o = s.options[0];
    if (!o || !/choose an action/i.test(o.text)) return false;
    const args = (s.getAttribute('onchange') || '').replace(/'[^']*'|"[^"]*"/g, '');
    const nums = args.match(/(?<![\w.])-?\d+(?![\w.])/g) || [];
    return Number(nums[argIndex]) === id;
});
if (!sels.length) return false;
const s = sels[0];
const opt = [...s.options].find(o => o.text.trim().toLowerCase() === 'view list');
if (!opt) return false;
s.value = opt.value;
s.dispatchEvent(new Event('change'));
"""

# Calendar: set the Display Events boxes, then Apply (the page's itemList()).
CALENDAR_VIEW_JS = r"""
const want = %s;
if (typeof w.itemList !== 'function') return false;
for (const [id, on] of Object.entries(want)) { const el = doc.getElementById(id); if (el) el.checked = on; }
w.itemList();
"""

# Calendar: go to page N of the event list. The pager is an ASP.NET partial
# postback (the list is replaced in place, no page load), started from the
# frame's own timer because its code fails when called from another script.
# Resolves once page N is the active page.
CALENDAR_PAGE_JS = r"""
if (typeof w.__doPostBack !== 'function') return false;
w.setTimeout("__doPostBack(" + JSON.stringify(%s) + ", '%d')", 0);
const want = '%d', t0 = Date.now();
return new Promise((ok, bad) => {
    const iv = setInterval(() => {
        try {
            const a = cur().querySelector('p.pagination span.active');
            if (a && a.textContent.trim() === want) { clearInterval(iv); ok(); }
            else if (Date.now() - t0 > 45000) { clearInterval(iv); bad('page ' + want + ' did not appear'); }
        } catch (e) {}
    }, 300);
});
"""

# Staff Directory: run the page's Expand Current View until every department
# with children has loaded them (the tree loads lazily), then resolve.
STAFF_EXPAND_JS = r"""
if (typeof w.expandAll !== 'function') return false;
w.setTimeout('expandAll(-1, 1, 0)', 0);
const t0 = Date.now();
let lastCount = -1, lastPending = -1, stable = 0, changedAt = Date.now(), kicks = 1;
return new Promise((ok, bad) => {
    const iv = setInterval(() => {
        const d = cur();
        const pending = [...d.querySelectorAll('a[id^=category]')]
            .filter(a => a.getAttribute('cnloaded') === '0' && Number(a.getAttribute('cn') || 0) > 0).length;
        const count = d.querySelectorAll('div[id^=div]').length;
        if (pending !== lastPending || count !== lastCount) changedAt = Date.now();
        // nothing has loaded for 10 s but some departments are still closed: expand again
        if (pending && Date.now() - changedAt > 10000 && kicks < 5) {
            kicks++; changedAt = Date.now(); w.setTimeout('expandAll(-1, 1, 0)', 0);
        }
        stable = (!pending && count === lastCount) ? stable + 1 : 0;
        lastCount = count; lastPending = pending;
        if (stable >= 2) { clearInterval(iv); ok(); }
        else if (Date.now() - t0 > 90000) { clearInterval(iv); bad(pending + ' department(s) still closed'); }
    }, 500);
});
"""

# Classic modules: which number in an item row's onchange is the item id when
# the row has no checkbox (FAQ questions: FAQQuestionModifyDelete(topic, question, this)).
ITEM_ID_ARG = {"faq": 1}


@dataclass
class SiteProfile:
    site: str
    modules: dict[str, dict] = field(default_factory=dict)   # key -> {present, label, html, url, error}

    def present(self) -> list[ModuleDef]:
        return [m for m in MODULES if self.modules.get(m.key, {}).get("present")]


class Scanner:
    def __init__(self, browser: Browser, parallel: int = 4,
                 on_module_done: Optional[Callable[[ModuleScan], None]] = None,
                 cache_dir: Optional[Path] = None, refresh_history: bool = False) -> None:
        self.b = browser
        self.r = browser.r
        self.parallel = parallel
        self.on_module_done = on_module_done
        self.profile: Optional[SiteProfile] = None
        self.history = HistoryLog(browser, parallel=max(parallel, 6), cache_dir=cache_dir,
                                  refresh=refresh_history)

    # ============================================================== profile
    def build_profile(self, modules: Optional[list[ModuleDef]] = None) -> SiteProfile:
        """Which modules this site has: load each fixed admin URL and check
        the page's module id. Also keeps each landing page for the scan."""
        modules = modules or MODULES
        prof = SiteProfile(self.b.site_url)
        newer = [m for m in modules if m.style == "newer"]
        classic = [m for m in modules if m.style != "newer"]
        for m, res in zip(newer, self.b.fetch_many([m.path for m in newer], parallel=self.parallel)):
            prof.modules[m.key] = self._landing(m, res.get("ok"), res.get("url"), res.get("text", ""),
                                                f"HTTP {res.get('status')}")
        jobs = [{"src": m.path} for m in classic]
        for m, res in zip(classic, self.b.frames(jobs, parallel=self.parallel)):
            html = res["pages"][-1] if res.get("pages") else ""
            prof.modules[m.key] = self._landing(m, res.get("ok"), res.get("url"), html, res.get("error"))
        self.profile = prof
        return prof

    def _landing(self, m: ModuleDef, ok, url, html, error) -> dict:
        found = parse.module_id_of(html) if ok else None
        present = found == m.module_id
        label = parse.page_title(html) if present else ""
        if label.lower() in ("", "civicplus"):
            label = m.label
        info = {"present": present, "label": label, "html": html if present else "", "url": url or ""}
        if not present:
            info["error"] = (error or "") if not ok else f"page is not this module (module id {found})"
        return info

    # ================================================================= scan
    def scan(self, keys: Optional[list[str]] = None, with_history: bool = True) -> list[ModuleScan]:
        wanted = [m for m in MODULES if not keys or m.key in keys]
        self.r.info(f"Checking which of {len(wanted)} module(s) this site has...")
        prof = self.build_profile(wanted)
        results = []
        for m in wanted:
            self.b.cancel.check()
            info = prof.modules[m.key]
            if not info["present"]:
                self.r.warn(f"{m.label}: not on this site ({info.get('error') or 'no page'}).")
                ms = ModuleScan(m.key, m.label, present=False)
            else:
                ms = self.scan_module(m, info, with_history)
            results.append(ms)
            if self.on_module_done:
                self.on_module_done(ms)
        return results

    def scan_module(self, m: ModuleDef, info: dict, with_history: bool = True) -> ModuleScan:
        t0 = time.monotonic()
        ms = ModuleScan(m.key, info["label"])
        self.r.info(f"{ms.label}: reading..." + ("" if not with_history else
                    " (History Log can take a few minutes the first time on a busy module)"))
        try:
            reader = getattr(self, f"_scan_{m.key}", None) or getattr(self, f"_scan_{m.style}")
            reader(m, info["html"], ms)
            for row in ms.rows:
                row.module_label = ms.label
            if with_history and ms.rows:
                try:
                    entries = self.history.module_history(m.module_id)
                except SiteError as e:
                    if self.b.on_login_page() or "off-site" in str(e):
                        raise
                    ms.problems.append(f"History Log couldn't be read, so Created is blank: {e}")
                    entries = []
                matched = join_history(ms.rows, entries)
                saved = getattr(self.history, "last_saved", 0)
                note = f" ({saved} saved from earlier scans)" if saved else ""
                self.r.info(f"{ms.label}: History Log has {len(entries)} row(s){note}; "
                            f"matched {matched} of {len(ms.rows)} record(s).")
                older = sum(1 for r in ms.rows if not r.created_by and "history_starts" in r.extra)
                missing = sum(1 for r in ms.rows if not r.created_by) - older
                for r in ms.rows:
                    if "history_starts" in r.extra:
                        r.detail = "; ".join(x for x in (r.detail, f"History starts {r.extra['history_starts']}") if x)
                if older:
                    ms.problems.append(f"{older} record(s) are older than their History Log entries "
                                       "(first entry isn't a create), so Created is blank")
                if missing:
                    ms.problems.append(f"{missing} record(s) have no History Log entries (Created is blank)")
        except SiteError as e:
            if self.b.on_login_page() or "off-site" in str(e):
                raise                  # logged out: stop the whole scan
            ms.problems.append(f"Scan failed: {e}")
            self.r.error(f"{ms.label}: scan failed: {e}")
        except Exception as e:     # one module failing shouldn't lose the others
            ms.problems.append(f"Scan failed: {e}")
            self.r.error(f"{ms.label}: scan failed: {e}")
        ms.seconds = time.monotonic() - t0
        cats = sum(1 for r in ms.rows if r.level == "category")
        items = len(ms.rows) - cats
        self.r.info(f"{ms.label}: {cats} categor{'y' if cats == 1 else 'ies'}, {items} item(s) "
                    f"in {ms.seconds:.0f}s.")
        for p in ms.problems:
            self.r.warn(f"{ms.label}: {p}")
        return ms

    # ----------------------------------------------------------- newer pages
    def _paged(self, url_for: Callable[[int, int], str], ids: list[int], parser) -> dict[int, list[dict]]:
        """Fetch page 1 for every id at once, then any further pages."""
        out: dict[int, list[dict]] = {i: [] for i in ids}
        first = self.b.fetch_many([url_for(i, 1) for i in ids], parallel=self.parallel)
        more: list[tuple[int, int]] = []
        for i, res in zip(ids, first):
            if not res.get("ok"):
                raise SiteError(f"Could not read {res['path']} (HTTP {res.get('status')})")
            data = parser(res["text"])
            out[i] += data["rows"]
            more += [(i, p) for p in range(2, data["max_page"] + 1)]
        if more:
            for (i, _), res in zip(more, self.b.fetch_many([url_for(i, p) for i, p in more],
                                                           parallel=self.parallel)):
                if not res.get("ok"):
                    raise SiteError(f"Could not read {res['path']} (HTTP {res.get('status')})")
                out[i] += parser(res["text"])["rows"]
        for i in ids:      # a row can repeat across pages if something changed mid-scan
            seen, unique = set(), []
            for row in out[i]:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    unique.append(row)
            out[i] = unique
        return out

    def _categories_newer(self, m: ModuleDef, html: str, ms: ModuleScan) -> list[dict]:
        cats = parse.newer_categories(html)
        for c in cats:
            ms.rows.append(InventoryRow(m.key, "category", c["name"], c["name"], c["status"],
                                        category_id=c["id"], detail=c["detail"]))
        return cats

    def _scan_agenda(self, m: ModuleDef, html: str, ms: ModuleScan) -> None:
        cats = self._categories_newer(m, html, ms)
        by_cat = self._paged(lambda c, p: f"/Admin/AgendaCenter/Agenda/AgendaList?categoryID={c}&page={p}",
                             [c["id"] for c in cats], parse.agenda_list)
        for c in cats:
            for a in by_cat[c["id"]]:
                detail = f"Meeting {a['meeting']}" + ("; has minutes" if a["minutes"] else "")
                ms.rows.append(InventoryRow(m.key, "item", c["name"], a["name"], a["status"],
                                            category_id=c["id"], item_id=a["id"], modified_on=a["updated"],
                                            detail=detail, extra={"meeting": a["meeting"]},
                                            fields={"Meeting date": parse.site_date(a["meeting"]),
                                                    "Minutes": "yes" if a["minutes"] else "no"}))

    def _scan_forms(self, m: ModuleDef, html: str, ms: ModuleScan) -> None:
        cats = self._categories_newer(m, html, ms)
        by_cat = self._paged(lambda c, p: f"/Admin/FormCenter/Item/ItemList?categoryID={c}&page={p}",
                             [c["id"] for c in cats], parse.form_list)
        for c in cats:
            for f in by_cat[c["id"]]:
                ms.rows.append(InventoryRow(m.key, "item", c["name"], f["name"], f["status"],
                                            category_id=c["id"], item_id=f["id"], modified_on=f["modified"],
                                            detail=f"{f['submissions'] or 0} submission(s)",
                                            fields={"Submissions": f["submissions"] or "0"}))

    def _scan_facilities(self, m: ModuleDef, html: str, ms: ModuleScan) -> None:
        cats = self._categories_newer(m, html, ms)
        by_cat = self._paged(lambda c, p: f"/Admin/Facilities/Category/Detail/{c}" + (f"?page={p}" if p > 1 else ""),
                             [c["id"] for c in cats], parse.facility_list)
        for c in cats:
            for f in by_cat[c["id"]]:
                detail = f"{f['reservations'] or 0} upcoming reservation(s)" + \
                         ("; has subfacilities" if f["has_children"] else "")
                ms.rows.append(InventoryRow(m.key, "item", c["name"], f["name"], f["status"],
                                            category_id=c["id"], item_id=f["id"], detail=detail,
                                            fields={"Upcoming reservations": f["reservations"] or "0",
                                                    "Subfacilities": "yes" if f["has_children"] else "no"}))

    # --------------------------------------------------------- classic pages
    @staticmethod
    def _fields(cols: dict, skip: set) -> dict:
        """A classic list's own columns, minus the name/status ones. Date
        columns are turned into YYYY-MM-DD so they sort and filter."""
        out = {}
        for k, v in cols.items():
            if k in skip:
                continue
            v = "" if v.lower().startswith("select ") else v
            out[k] = parse.site_date(v) if "date" in k.lower() and v else v
        return out

    @staticmethod
    def _detail(cols: dict, skip: set) -> str:
        return "; ".join(f"{k}: {v}" for k, v in cols.items() if v and k not in skip)

    def _scan_classic(self, m: ModuleDef, html: str, ms: ModuleScan) -> None:
        cats = parse.classic_rows(html)
        for c in cats:
            ms.rows.append(InventoryRow(m.key, "category", c["name"], c["name"], parse.classic_status(c),
                                        category_id=c["id"],
                                        modified_by=c["cols"].get("Last Modified By", ""),
                                        detail=self._detail(c["cols"], {"Status", "Last Modified By"} |
                                                            {k for k, v in c["cols"].items() if v == c["name"]}),
                                        fields=self._fields(c["cols"], {"Status", "Last Modified By"} |
                                                            {k for k, v in c["cols"].items() if v == c["name"]})))
        jobs = [{"src": m.path, "steps": [VIEW_LIST_JS % (c["id"], 0)]} for c in cats]
        arg = ITEM_ID_ARG.get(m.key, 0)
        for c, res in zip(cats, self.b.frames(jobs, parallel=self.parallel)):
            if not res.get("ok"):
                ms.problems.append(f"Could not open the list for category {c['name']!r}: {res.get('error')}")
                continue
            page = res["pages"][-1]
            if parse.module_id_of(page) != m.module_id:
                ms.problems.append(f"List for category {c['name']!r} opened the wrong page")
                continue
            if re.search(r"Next\s+\d+\s*(&gt;|>)", page):
                ms.problems.append(f"Category {c['name']!r} has more than one page of items; "
                                   "only the first page was read")
            for it in parse.classic_rows(page, id_arg=arg):
                ms.rows.append(InventoryRow(
                    m.key, "item", c["name"], it["name"], parse.classic_status(it),
                    category_id=c["id"], item_id=it["id"],
                    modified_by=it["cols"].get("Last Modified By", ""),
                    detail=self._detail(it["cols"], {"Status", "Last Modified By"} |
                                        {k for k, v in it["cols"].items() if v == it["name"]}),
                    fields=self._fields(it["cols"], {"Status", "Last Modified By"} |
                                        {k for k, v in it["cols"].items() if v == it["name"]})))
        self.r.info(f"{ms.label}: archived categories and items are not read yet "
                    "(the site hides them behind Show Archived).")

    # ------------------------------------------------------- module quirks
    def _scan_calendar(self, m: ModuleDef, html: str, ms: ModuleScan) -> None:
        # /admin/Calendar.aspx opens the calendar last viewed (a cookie);
        # ?show=all is the All Calendars list with every calendar's count.
        res = self.b.frames([{"src": "/admin/calendar.aspx?show=all"}], parallel=1)[0]
        cals = parse.calendar_list(res["pages"][-1]) if res.get("ok") else []
        if not cals:
            cals = parse.calendar_list(html)
        for c in cals:
            detail = f"{c['events']} event(s)" if c["events"] is not None else ""
            ms.rows.append(InventoryRow(m.key, "category", c["name"], c["name"], c["status"],
                                        category_id=c["id"], detail=detail,
                                        fields={"Events": "" if c["events"] is None else str(c["events"])}))
        if not cals:
            return
        # The event list hides past events unless "Previous" is ticked in the
        # Calendar's Display Events options, and those options are saved per
        # user. Tick everything while reading, then put back what was there.
        first = self.b.frames([{"src": f"/admin/calendar.aspx?CID={cals[0]['id']}"}], parallel=1)[0]
        original = parse.calendar_view_options(first["pages"][-1]) if first.get("ok") else {}
        if not original:
            ms.problems.append("Couldn't read the Calendar's Display Events options; events not read")
            return
        want = {k: (k != "ShowMineOnly") for k in original}
        changed = want != original
        if changed:
            self.r.info(f"{ms.label}: ticking Previous and every status in your Calendar display options "
                        "so past and unpublished events are listed; they're put back afterwards.")
        try:
            jobs = [{"src": f"/admin/calendar.aspx?CID={c['id']}", "steps": [CALENDAR_VIEW_JS % json.dumps(want)]}
                    for c in cals]
            pages: dict[int, list[str]] = {}
            more = []
            for c, res in zip(cals, self.b.frames(jobs, parallel=self.parallel, settle=1.5)):
                if not res.get("ok"):
                    ms.problems.append(f"Could not open calendar {c['name']!r}: {res.get('error')}")
                    continue
                pages[c["id"]] = [res["pages"][-1]]
                d = parse.calendar_events(res["pages"][-1])
                if d["max_page"] > 1:
                    more.append((c, d["pager"], d["max_page"]))
            jobs = [{"src": f"/admin/calendar.aspx?CID={c['id']}", "capture": "each",
                     "steps": [CALENDAR_PAGE_JS % (json.dumps(pager), p, p) for p in range(2, last + 1)]}
                    for c, pager, last in more]
            for (c, _, _), res in zip(more, self.b.frames(jobs, parallel=self.parallel, settle=1.5)):
                if not res.get("ok"):
                    ms.problems.append(f"Could not read every page of calendar {c['name']!r}: {res.get('error')}")
                pages[c["id"]] += res.get("pages", [])[1:]
        finally:
            if changed:
                res = self.b.frames([{"src": f"/admin/calendar.aspx?CID={cals[0]['id']}",
                                      "steps": [CALENDAR_VIEW_JS % json.dumps(original)]}], parallel=1)[0]
                back = parse.calendar_view_options(res["pages"][-1]) if res.get("ok") else {}
                if back == original:
                    self.r.info(f"{ms.label}: display options put back as they were.")
                else:
                    ms.problems.append("Couldn't put the Calendar display options back; check Previous / "
                                       "statuses in the Calendar's Display Events")
        for c in cals:
            seen: dict[int, dict] = {}
            all_ids: set[int] = set()
            total = None
            for html_page in pages.get(c["id"], []):
                d = parse.calendar_events(html_page)
                all_ids |= d["all_ids"]
                total = d["total"] if d["total"] is not None else total
                for e in d["rows"]:
                    seen.setdefault(e["id"], e)
            for e in seen.values():
                detail = e["when"] + (" (past)" if e["past"] else "")
                start, _, end = re.sub(r"\s*[–—]\s*", " - ", parse.clean(e["when"])).partition(" - ")
                ms.rows.append(InventoryRow(m.key, "item", c["name"], e["name"], e["status"],
                                            category_id=c["id"], item_id=e["id"], detail=detail,
                                            fields={"Starts": parse.site_date(start), "Ends": parse.site_date(end),
                                                    "Past": "yes" if e["past"] else "no"}))
            expected = len(all_ids) or total or 0
            if c["id"] in pages and expected and len(seen) != expected:
                ms.problems.append(f"Calendar {c['name']!r}: the site lists {expected} event(s); "
                                   f"{len(seen)} were read")

    def _scan_resdir(self, m: ModuleDef, html: str, ms: ModuleScan) -> None:
        rows = parse.classic_rows(html)
        # 100 per page, paged separately for unpublished (section 0) and published (1)
        totals = parse.resdir_totals(html)
        jobs = [{"src": m.path, "steps": [f"if (typeof w.changePage !== 'function') return false; "
                                          f"w.changePage({p}, {sec});"]}
                for sec, total in sorted(totals.items()) for p in range(2, -(-total // 100) + 1)]
        for job, res in zip(jobs, self.b.frames(jobs, parallel=self.parallel)):
            if not res.get("ok"):
                ms.problems.append(f"Could not read a Resource Directory page: {res.get('error')}")
                continue
            rows += parse.classic_rows(res["pages"][-1])
        seen: set[int] = set()
        rows = [r for r in rows if not (r["id"] in seen or seen.add(r["id"]))]
        expected = sum(totals.values())
        if expected and len(rows) != expected:
            ms.problems.append(f"The site says {expected} resources; {len(rows)} were read")
        for it in rows:
            ms.rows.append(InventoryRow(m.key, "item", "", it["name"], parse.classic_status(it),
                                        item_id=it["id"],
                                        detail=self._detail(it["cols"], {"Status"} |
                                                            {k for k, v in it["cols"].items() if v == it["name"]}),
                                        fields=self._fields(it["cols"], {"Status"} |
                                                            {k for k, v in it["cols"].items() if v == it["name"]})))

    def _scan_staff(self, m: ModuleDef, html: str, ms: ModuleScan) -> None:
        """Departments from the tree (ids, status, nesting), expanded in a
        hidden frame with the page's own Expand Current View. Employees from
        the module's own Export Staff workbook (it lists every department and
        employee, but without ids)."""
        res = self.b.frames([{"src": m.path, "steps": [STAFF_EXPAND_JS]}], parallel=1, settle=2,
                            page_timeout=120)[0]
        if not res.get("ok"):
            ms.problems.append(f"Couldn't expand the department tree: {res.get('error')}")
            tree = parse.staff_tree(res["pages"][-1]) if res.get("pages") else []
        else:
            tree = parse.staff_tree(res["pages"][-1])
        for d in tree:
            ms.rows.append(InventoryRow(m.key, "category", " > ".join(d["path"] + [d["name"]]), d["name"],
                                        d["status"], category_id=d["id"],
                                        detail=" > ".join(d["path"]) and f"In {' > '.join(d['path'])}",
                                        extra={"parent_id": d.get("parent")},
                                        fields={"Parent department": " > ".join(d["path"])}))
        try:
            data, _ = self.b.fetch_bytes("/Admin/Directory/Admin/ExportAllExcel")
            export = parse.staff_export(data)
        except Exception as e:
            ms.problems.append(f"Couldn't read Export Staff: {e}")
            return
        if len(export["departments"]) != len(tree):
            ms.problems.append(f"Export Staff lists {len(export['departments'])} department(s); "
                               f"the tree showed {len(tree)}")
        for e in export["employees"]:
            first = e.get("First Name") or e.get("FirstName") or ""
            last = e.get("Last Name") or e.get("LastName") or ""
            name = " ".join(x for x in (first, last) if x) or e.get("Name") or e.get("Full Name") or "(no name)"
            dept = next((e[k] for k in e if k.lower().startswith("department") and e[k]), "")
            title = e.get("Title") or e.get("Job Title") or ""
            ms.rows.append(InventoryRow(m.key, "item", dept, name, "",
                                        detail="; ".join(x for x in (title, e.get("Email", "")) if x),
                                        fields={"Title": title, "Email": e.get("Email", ""),
                                                "Phone": e.get("Phone") or e.get("Work Phone") or ""}))
        if export["employees"]:
            ms.problems.append("Employees come from Export Staff, which has no IDs, so they have no "
                               "Created / Created By and no status yet")
