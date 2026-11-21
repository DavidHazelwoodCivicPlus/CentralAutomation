"""Publish, unpublish and delete for every module except Agenda Center
(which has its own jobs in agendacenter/jobs.py), run from the window.

Same rules as Agenda Center:
  * Every job runs as a dry run first; a live run needs the site's address typed.
  * Live runs write results.csv and undo.json (Reporter does both).
  * Publishing never notifies anyone: every post carries the Don't Send values.
  * A ticked category means the whole category: Delete clears everything in
    it first (the site refuses to delete a category with content), Publish
    also publishes its unpublished items. Unpublish only unpublishes the
    category itself (that hides everything in it).

Speed: requests go from inside the logged-in page, IDs in bulk where the site
takes them (actions/relay.js), one request at a time. Nothing is read again
after each request; one read at the end reports anything still there.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..models import InventoryRow
from ..modules import get as get_module
from ..reporting import ActionResult, Reporter
from .engine import RELAY_KEY, Relay

# What the site calls a category and an item, per module (for the results).
WORDS = {
    "archive": ("archive", "archive item"), "bids": ("category", "bid"), "calendar": ("calendar", "event"),
    "facilities": ("category", "facility"), "faq": ("topic", "question"), "forms": ("category", "form"),
    "resdir": ("category", "resource"), "news": ("category", "news flash"), "staff": ("department", "employee"),
    "pages": ("page", "link"), "docs": ("folder", "file"),
}

# Pages the tool never unpublishes or deletes: the home page (and, below, the
# layout pages the site's templates are built from).
PROTECTED_PAGES = {1}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def same_person(a: str, b: str) -> bool:
    """Export Staff says "First Last"; the department tree says "Last, First"."""
    def key(s: str) -> str:
        s = _norm(s)
        if "," in s:
            last, _, first = s.partition(",")
            s = f"{first.strip()} {last.strip()}"
        return s
    return key(a) == key(b)


@dataclass
class Selection:
    """Ticked rows of one module tab: whole categories, and items in
    categories that aren't ticked themselves."""

    module: str
    categories: dict[int, str] = field(default_factory=dict)          # id -> name
    items: dict[int, dict[int, str]] = field(default_factory=dict)    # category id (0 = none) -> {id: name}
    unresolved: list[InventoryRow] = field(default_factory=list)      # items without an id (Staff Directory)

    @classmethod
    def from_rows(cls, module: str, rows: list[InventoryRow]) -> "Selection":
        sel = cls(module)
        if module == "pages":            # links, widgets and images aren't things to publish or delete
            rows = [r for r in rows if r.level == "category"]
        for r in rows:
            if r.level == "category" and r.category_id is not None:
                sel.categories[r.category_id] = r.name
        for r in rows:
            if r.level != "item" or (r.category_id is not None and r.category_id in sel.categories):
                continue
            if r.item_id is None:
                sel.unresolved.append(r)
            else:
                sel.items.setdefault(r.category_id or 0, {})[r.item_id] = r.name
        return sel

    def item_count(self) -> int:
        return sum(len(v) for v in self.items.values()) + len(self.unresolved)

    def describe(self) -> str:
        cw, iw = WORDS.get(self.module, ("category", "item"))
        parts = []
        if self.categories:
            parts.append(f"{len(self.categories)} {cw}{'' if len(self.categories) == 1 else 's'}")
        if self.item_count():
            parts.append(f"{self.item_count()} {iw}{'' if self.item_count() == 1 else 's'}")
        return " and ".join(parts) or "nothing"


class ModuleJobs:
    """The jobs one module tab runs. Same interface as AgendaJobs for the run window."""

    def __init__(self, browser, reporter: Reporter, live: bool, key: str) -> None:
        browser.r = reporter
        self.b = browser
        self.key = key
        self.mod = RELAY_KEY[key]
        self.label = get_module(key).label
        self.cw, self.iw = WORDS.get(key, ("category", "item"))
        self.relay = Relay(browser)
        self.r = reporter
        self.live = live
        self._cats: Optional[dict[int, dict]] = None
        self._want: set[int] = set()     # Pages: the pages whose status is read

    # ------------------------------------------------------------- results
    def _res(self, action: str, level: str, cat: str, tid, name: str, status: str, detail: str = "") -> None:
        self.r.result(ActionResult(step=action, action=action, target_type=self.cw if level == "category" else self.iw,
                                   category=cat, target_id=str(tid or ""), description=name,
                                   status=status, detail=detail))

    def _undo(self, action: str, level: str, tid: int, name: str, cid: Optional[int] = None, cat: str = "") -> None:
        self.r.record_undo({"module": self.key, "undo": action, "level": level, "id": tid, "name": name,
                            "category_id": cid, "category": cat})

    def cats(self, refresh: bool = False) -> dict[int, dict]:
        if self._cats is None or refresh:
            self._cats = {int(c["id"]): c for c in self.relay.cats(self.mod)}
            if self.key == "pages":
                for c in self._cats.values():
                    c["kind"] = "layout" if "Layout-Page" in c["name"] else ""
                # The page list has no statuses: read the pages this job is about.
                want = [i for i in self._want if i in self._cats]
                for pg in (self.relay.call("pages", "read", want) if want else []):
                    c = self._cats[int(pg["id"])]
                    st = pg.get("status") or ""
                    c.update(name="Home" if c["id"] == 1 else (pg.get("title") or c["name"]), status=st, parent=pg.get("parent"),
                             depth=len(pg.get("path") or []),
                             published=True if st == "published" else (None if st in ("", "not found") else False))
        return self._cats

    def _protected(self, c: dict) -> str:
        if self.key == "docs":
            return "a top folder (Document Center, Public Images) is never changed by the tool" if c.get("top") else ""
        if self.key != "pages":
            return ""
        if c["id"] in PROTECTED_PAGES:
            return "the home page is never unpublished or deleted by the tool"
        if c.get("kind") == "layout":
            return "a layout page (the site's templates use it) is never unpublished or deleted by the tool"
        return ""

    def _items(self, cid: int) -> dict[int, dict]:
        return {int(i["id"]): i for i in self.relay.items(self.mod, cid)}

    def _cat_name(self, cid: int) -> str:
        return (self.cats().get(cid) or {}).get("name", "") if cid else ""

    def _timed(self, what: str, t0: float) -> None:
        self.r.info(f"{self.label}: {what} in {time.monotonic() - t0:.1f}s "
                    f"({self.relay.calls} request(s) so far, {self.relay.seconds:.1f}s in requests).")

    def _resolve(self, sel: Selection) -> None:
        """Staff Directory employees come from Export Staff without IDs: find
        them in their department on the site by name."""
        if not sel.unresolved:
            return
        by_name: dict[str, list[int]] = {}
        for cid, c in self.cats().items():
            by_name.setdefault(_norm(c["name"]), []).append(cid)
        listed: dict[int, dict[int, dict]] = {}
        for r in sel.unresolved:
            ids = by_name.get(_norm(r.category), [])
            if len(ids) != 1:
                self._res("find", "item", r.category, "", r.name, "skipped",
                          f"department {r.category!r} " + ("isn't on the site" if not ids else "has more than one match"))
                continue
            cid = ids[0]
            if cid not in listed:
                listed[cid] = self._items(cid)
            hits = [i for i in listed[cid].values() if same_person(i["name"], r.name)]
            if len(hits) != 1:
                self._res("find", "item", r.category, "", r.name, "skipped",
                          "not found in that department" if not hits else "more than one person has this name there")
                continue
            sel.items.setdefault(cid, {})[int(hits[0]["id"])] = r.name
        sel.unresolved = []

    # ================================================================ jobs
    def set_published(self, sel: Selection, publish: bool) -> None:
        action = "publish" if publish else "unpublish"
        t0 = time.monotonic()
        self._want = set(sel.categories)
        cats = self.cats(refresh=True)
        self._resolve(sel)
        expect_items: dict[int, dict[int, bool]] = {}
        # Items in categories that aren't ticked.
        for cid, wanted in sel.items.items():
            cname = self._cat_name(cid)
            have = self._items(cid)
            todo = []
            for iid, name in wanted.items():
                it = have.get(iid)
                if it is None:
                    self._res(action, "item", cname, iid, name, "skipped", "not on the site any more")
                elif it.get("published") == publish:
                    self._res(action, "item", cname, iid, it["name"] or name, "skipped", f"already {action}ed")
                else:
                    todo.append(it)
            self._item_step(action, cid, cname, todo, expect_items)
        # Whole categories (and, for Publish, every unpublished item in them).
        cat_todo = []
        for cid, name in sel.categories.items():
            c = cats.get(cid)
            if c is None:
                self._res(action, "category", name, cid, name, "skipped", "not on the site any more")
            elif c.get("published") == publish:
                self._res(action, "category", c["name"], cid, c["name"], "skipped", f"already {action}ed")
            elif not publish and self._protected(c):
                self._res(action, "category", c["name"], cid, c["name"], "skipped", self._protected(c))
            else:
                cat_todo.append(c)
        self._cat_step(action, cat_todo)
        if publish and self.key != "pages":
            for cid in sel.categories:
                if cid not in cats:
                    continue
                drafts = [i for i in self._items(cid).values() if i.get("published") is False]
                if drafts:
                    self.r.info(f"{cats[cid]['name']}: {len(drafts)} unpublished {self.iw}(s) to publish.")
                self._item_step(action, cid, cats[cid]["name"], drafts, expect_items)
        self._timed(f"{action} done", t0)
        if self.live:
            self._check({c["id"]: publish for c in cat_todo}, expect_items)

    def _item_step(self, action: str, cid: int, cname: str, todo: list[dict],
                   expect: dict[int, dict[int, bool]]) -> None:
        if not todo:
            return
        note = " (Notify Me: Don't Send)" if action == "publish" else ""
        if not self.live:
            for it in todo:
                self._res(action, "item", cname, it["id"], it["name"], "dry-run", f"would {action}{note}")
            return
        t0 = time.monotonic()
        ok, failed = self.relay.act(self.mod, "item", action, [it["id"] for it in todo], cid)
        okset = set(ok)
        for it in todo:
            if it["id"] in okset:
                self._res(action, "item", cname, it["id"], it["name"], "ok", note.strip(" ()"))
                self._undo("unpublish" if action == "publish" else "publish", "item", it["id"], it["name"], cid, cname)
                expect.setdefault(cid, {})[it["id"]] = action == "publish"
            else:
                self._res(action, "item", cname, it["id"], it["name"], "error", failed.get(it["id"], "not done"))
        self.r.info(f"{cname or self.label}: {action} {len(ok)} {self.iw}(s) in {time.monotonic() - t0:.1f}s.")

    def _cat_step(self, action: str, todo: list[dict], quiet: bool = False) -> list[int]:
        """quiet: a step inside a delete (unpublish first): no row or undo for it."""
        if not todo:
            return []
        if not self.live:
            for c in todo:
                self._res(action, "category", c["name"], c["id"], c["name"], "dry-run", f"would {action}")
            return []
        t0 = time.monotonic()
        ok, failed = self.relay.act(self.mod, "category", action, [c["id"] for c in todo],
                                    extra=self._parents([c["id"] for c in todo]))
        okset = set(ok)
        for c in todo:
            if c["id"] in okset:
                if action != "delete" and not quiet:
                    self._res(action, "category", c["name"], c["id"], c["name"], "ok")
                    self._undo("unpublish" if action == "publish" else "publish", "category", c["id"], c["name"])
            else:
                self._res(action, "category", c["name"], c["id"], c["name"], "error", failed.get(c["id"], "not done"))
        self.r.info(f"{self.label}: {action} {len(ok)} {self.cw}(s) in {time.monotonic() - t0:.1f}s.")
        return ok

    def _parents(self, ids: list[int]) -> Optional[dict]:
        if self.key != "staff":
            return None
        return {str(i): (self.cats().get(i) or {}).get("parent", 0) for i in ids}

    # -------------------------------------------------------------- delete
    def delete(self, sel: Selection, known: Optional[dict[int, list[dict]]] = None) -> None:
        """Delete ticked items (unpublishing first), then ticked categories
        with everything in them. known: item lists already read this run."""
        t0 = time.monotonic()
        self._want = set(sel.categories)
        cats = self.cats(refresh=True)
        self._resolve(sel)
        expect_items: dict[int, dict[int, Optional[bool]]] = {}
        for cid, wanted in sel.items.items():
            cname = self._cat_name(cid)
            have = ({int(i["id"]): i for i in known[cid]} if known and cid in known else self._items(cid))
            todo = []
            for iid, name in wanted.items():
                if iid in have:
                    todo.append(have[iid])
                else:
                    self._res("delete", "item", cname, iid, name, "skipped", "not on the site any more")
            self._delete_items(cid, cname, todo, expect_items)
        # Categories: a department inside another ticked department goes with it.
        picked = []
        for cid, name in sel.categories.items():
            if cid not in cats:
                self._res("delete", "category", name, cid, name, "skipped", "not on the site any more")
            elif self._protected(cats[cid]):
                self._res("delete", "category", cats[cid]["name"], cid, cats[cid]["name"], "skipped",
                          self._protected(cats[cid]))
            else:
                picked.append(cats[cid])
        if self.key in ("staff", "docs"):
            # A department (folder) goes with its sub-departments (sub-folders) (whatever their depth),
            # deepest first, so each is empty when it's deleted.
            have = {c["id"] for c in picked}
            for c in cats.values():
                if c["id"] not in have and any(a in have for a in self._ancestors(c["id"])):
                    picked.append(c)
            have = {c["id"] for c in picked}
            for c in list(picked):
                if c["id"] not in sel.categories:
                    self.r.info(f"{c['name']}: inside a ticked department, so it goes too.")
            depth = {c["id"]: self._depth(c["id"]) for c in picked}
            picked.sort(key=lambda c: -depth[c["id"]])
        if self.key == "pages":
            # Sub-pages before their parents: the site won't delete a page that still has sub-pages.
            picked.sort(key=lambda c: -(c.get("depth") or 0))
        contents = {}
        for c in picked:
            contents[c["id"]] = list(self._items(c["id"]).values())
        if not self.live:
            for c in picked:
                n = len(contents[c["id"]])
                pub = sum(1 for i in contents[c["id"]] if i.get("published"))
                detail = ("would empty it (the site's built-in sample category can't be deleted)" if c.get("sample") else
                          "would delete" + (" (unpublish it first)" if c.get("published") is not False and self.key not in ("pages", "docs") else ""))
                if n:
                    detail += f"; first delete its {n} {self.iw}(s)" + (f" ({pub} published: unpublished first)" if pub else "")
                self._res("delete", "category", c["name"], c["id"], c["name"], "dry-run", detail)
            self._timed("dry run done", t0)
            return
        for c in picked:
            self._delete_items(c["id"], c["name"], contents[c["id"]], expect_items, quiet=True)
        # Form Center's built-in Sample Forms category: emptied, but the site never deletes it.
        for c in [c for c in picked if c.get("sample")]:
            n = len(contents[c["id"]])
            self._res("delete", "category", c["name"], c["id"], c["name"], "skipped",
                      "the site's built-in sample category can't be deleted" + (f"; its {n} {self.iw}(s) were deleted" if n else ""))
        picked = [c for c in picked if not c.get("sample")]
        if picked:
            pub = [c for c in picked if c.get("published") is not False and self.key not in ("pages", "docs")]
            if pub:
                self._cat_step("unpublish", pub, quiet=True)
            done = set(self._cat_step("delete", picked))
            self._timed("delete done", t0)
            gone = self._check({c["id"]: None for c in picked if c["id"] in done}, expect_items)
            for c in picked:
                if c["id"] not in done:
                    continue
                n = len(contents[c["id"]])
                if c["id"] in gone:
                    self._res("delete", "category", c["name"], c["id"], c["name"], "ok",
                              f"with its {n} {self.iw}(s)" if n else "")
                else:
                    left = self._items(c["id"])
                    self._res("delete", "category", c["name"], c["id"], c["name"], "error",
                              "still on the site" + (f": {len(left)} {self.iw}(s) couldn't be removed "
                                                     f"(e.g. {', '.join(i['name'] for i in list(left.values())[:3])})"
                                                     if left else " (the site didn't delete it; it may hold archived "
                                                                  "or hidden items)"))
        else:
            self._timed("delete done", t0)
            if expect_items:
                self._check({}, expect_items)

    def _ancestors(self, cid: int) -> list[int]:
        out, p = [], (self.cats().get(cid) or {}).get("parent")
        while p and len(out) < 20:
            out.append(int(p))
            p = (self.cats().get(p) or {}).get("parent")
        return out

    def _depth(self, cid: int) -> int:
        return len(self._ancestors(cid))

    def _delete_items(self, cid: int, cname: str, todo: list[dict], expect: dict, quiet: bool = False) -> None:
        if not todo:
            return
        if not self.live:
            for it in todo:
                self._res("delete", "item", cname, it["id"], it["name"], "dry-run",
                          "would unpublish, then delete" if it.get("published") else "would delete")
            return
        t0 = time.monotonic()
        # Documents and images are deleted as they are (the site's own Delete does that).
        pub = [it["id"] for it in todo if it.get("published") is not False] if self.key != "docs" else []
        failed: dict[int, str] = {}
        if pub:
            _, f = self.relay.act(self.mod, "item", "unpublish", pub, cid)
            failed.update({k: f"unpublish: {v}" for k, v in f.items()})
        ok, f = self.relay.act(self.mod, "item", "delete", [it["id"] for it in todo if it["id"] not in failed], cid)
        failed.update(f)
        okset = set(ok)
        for it in todo:
            if it["id"] in okset:
                if not quiet:
                    self._res("delete", "item", cname, it["id"], it["name"], "ok")
                expect.setdefault(cid, {})[it["id"]] = None
            else:
                self._res("delete", "item", cname, it["id"], it["name"], "error", failed.get(it["id"], "not done"))
        self.r.info(f"{cname or self.label}: deleted {len(ok)} {self.iw}(s) in {time.monotonic() - t0:.1f}s.")

    def delete_everything(self) -> None:
        """Every category with everything in it, and (Resource Directory)
        every entry."""
        cats = self.cats(refresh=True)
        sel = Selection(self.key, categories={cid: c["name"] for cid, c in cats.items() if not c.get("top")})
        known = {}
        if self.key == "resdir":
            known[0] = self.relay.items(self.mod, 0)
            sel.items[0] = {int(i["id"]): i["name"] for i in known[0]}
        self.r.info(f"{self.label}: {len(sel.categories)} {self.cw}(s)"
                    + (f" and {len(sel.items.get(0, {}))} {self.iw}(s)" if self.key == "resdir" else "")
                    + " on the site.")
        self.delete(sel, known)

    # ------------------------------------------------------- one read at the end
    def _check(self, cats_expect: dict[int, Optional[bool]], items_expect: dict[int, dict[int, Optional[bool]]]) -> set[int]:
        """One read of the module (and of each category whose items were
        changed) after the run: anything not as expected is reported.
        Returns the category IDs that are gone."""
        t0 = time.monotonic()
        now = self.cats(refresh=True)
        gone = {cid for cid in cats_expect if cid not in now}
        problems = 0
        for cid, want in cats_expect.items():
            if want is not None and cid in now and now[cid].get("published") not in (want, None):
                problems += 1
                self._res("check", "category", now[cid]["name"], cid, now[cid]["name"], "error",
                          f"still {'unpublished' if want else 'published'} after the run")
        for cid, want in items_expect.items():
            if cid and cid not in now:
                continue                         # the category itself went
            have = self._items(cid)
            for iid, state in want.items():
                it = have.get(iid)
                if state is None and it is not None:
                    problems += 1
                    self._res("check", "item", self._cat_name(cid), iid, it["name"], "error", "still on the site")
                elif state is not None and it is not None and it.get("published") not in (state, None):
                    problems += 1
                    self._res("check", "item", self._cat_name(cid), iid, it["name"], "error",
                              f"still {'unpublished' if state else 'published'}")
        self.r.info(f"{self.label}: checked the site in {time.monotonic() - t0:.1f}s: "
                    + ("everything as expected." if not problems else f"{problems} thing(s) not as expected (see the list)."))
        return gone

    # -------------------------------------------------------------- create
    def create_categories(self, names: list[str], publish: bool) -> None:
        t0 = time.monotonic()
        have = {_norm(c["name"]) for c in self.cats(refresh=True).values()}
        made = []
        for name in names:
            name = re.sub(r"\s+", " ", name).strip()
            if not name:
                continue
            if self.key == "calendar" and len(name) > 50:
                self._res("create", "category", name, "", name, "error", "Calendar names are limited to 50 characters")
                continue
            if _norm(name) in have:
                self._res("create", "category", name, "", name, "skipped", "already exists (or listed twice)")
                continue
            have.add(_norm(name))
            if not self.live:
                self._res("create", "category", name, "", name, "dry-run",
                          "would create" + (" and publish" if publish else " as unpublished"))
                continue
            try:
                r = self.relay.create(self.mod, name, publish)
                made.append((name, r.get("id")))
            except Exception as e:
                self._res("create", "category", name, "", name, "error", str(e))
        if made:
            now = self.cats(refresh=True)
            by = {}
            for c in now.values():
                by.setdefault(_norm(c["name"]), []).append(c)
            for name, cid in made:
                hit = now.get(cid) if cid else (by.get(_norm(name)) or [None])[-1]
                if hit is None:
                    self._res("create", "category", name, "", name, "error", "saved, but it isn't in the list afterwards")
                    continue
                self._res("create", "category", name, hit["id"], name, "ok",
                          "published" if hit.get("published") else "unpublished")
                self._undo("delete", "category", hit["id"], name)
        self._timed("create done", t0)

    # ---------------------------------------------------------------- undo
    def undo(self, entries: list[dict]) -> None:
        """Put back what a live run changed: publish what it unpublished,
        unpublish what it published, delete categories it created."""
        mine = [e for e in entries if e.get("module") == self.key]
        self._want = {int(e["id"]) for e in mine if e.get("level") == "category"}
        cats = self.cats(refresh=True)
        expect_cats: dict[int, Optional[bool]] = {}
        expect_items: dict[int, dict[int, Optional[bool]]] = {}
        by_action: dict[tuple[str, str], list[dict]] = {}
        for e in mine:
            by_action.setdefault((e["undo"], e["level"]), []).append(e)
        for (action, level), es in sorted(by_action.items(), key=lambda kv: kv[0][1] == "category"):
            if level == "category":
                todo = []
                for e in es:
                    c = cats.get(int(e["id"]))
                    if c is None:
                        self._res(action, "category", e["name"], e["id"], e["name"], "skipped", "not on the site any more")
                    else:
                        todo.append(c)
                if action == "delete":
                    self.delete(Selection(self.key, categories={c["id"]: c["name"] for c in todo}))
                else:
                    for cid in self._cat_step(action, todo):
                        expect_cats[cid] = action == "publish"
            else:
                groups: dict[int, list[dict]] = {}
                for e in es:
                    groups.setdefault(int(e.get("category_id") or 0), []).append(e)
                for cid, ges in groups.items():
                    have = self._items(cid)
                    todo = [have[int(e["id"])] for e in ges if int(e["id"]) in have]
                    for e in ges:
                        if int(e["id"]) not in have:
                            self._res(action, "item", e.get("category", ""), e["id"], e["name"], "skipped",
                                      "not on the site any more")
                    self._item_step(action, cid, ges[0].get("category", ""), todo, expect_items)
        if self.live and (expect_cats or expect_items):
            self._check(expect_cats, expect_items)


def load_undo(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
