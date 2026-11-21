"""Ordered multi-step runs.

A plan is a list of steps, run top to bottom, e.g.

    1. delete_agendas      in "Old Board"
    2. delete_categories   "Old Board"
    3. create_categories   "New Board"
    4. upload_agendas      from the workbook into "New Board"

Plans are plain JSON so the CLI menu can build them, people can keep and
re-use them, and a GUI can produce the same thing later. Every run is a
dry run unless live=True; a dry run reads the site but changes nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .filters import AgendaFilter, ItemFilter, select_agendas, select_items
from .models import Agenda, Category, Item
from ..reporting import ActionResult, Reporter
from .site import AgendaCenterSite

AGENDA_ACTIONS = ("scan_agendas", "unpublish_agendas", "delete_agendas", "publish_agendas")
ITEM_ACTIONS = ("scan_items", "approve_items", "delete_items")
CATEGORY_ACTIONS = ("publish_categories", "unpublish_categories", "delete_categories")
ALL_ACTIONS = AGENDA_ACTIONS + ITEM_ACTIONS + CATEGORY_ACTIONS + ("create_categories",)
DESTRUCTIVE = {"delete_agendas", "delete_items", "delete_categories"}


def delete_contents_first(params: dict) -> bool:
    """delete_categories: also delete the category's agendas, items, default
    items, item folders and workflows (the site refuses to delete a category
    that still has any). `delete_agendas_first` is the older name."""
    return bool(params.get("delete_contents_first", params.get("delete_agendas_first")))

MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class Step:
    action: str
    params: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        data = dict(data)
        action = data.pop("action")
        if action not in ALL_ACTIONS:
            raise ValueError(f"Unknown step action {action!r}; use one of {list(ALL_ACTIONS)}")
        return cls(action, data)

    def to_dict(self) -> dict:
        return {"action": self.action, **self.params}

    def describe(self) -> str:
        p = self.params
        if self.action in AGENDA_ACTIONS:
            return f"{self.action}: " + AgendaFilter.from_dict(p.get("filter")).describe()
        if self.action in ITEM_ACTIONS:
            return f"{self.action}: " + ItemFilter.from_dict(p.get("filter")).describe()
        if self.action in CATEGORY_ACTIONS:
            extra = (" (first delete their agendas, items, default items, folders and workflows)"
                     if self.action == "delete_categories" and delete_contents_first(p) else "")
            return f"{self.action}: " + (", ".join(p.get("categories") or []) or "ALL categories") + extra
        if self.action == "create_categories":
            names = p.get("names") or []
            shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
            return (f"create_categories ({len(names)}, skipping any already on the site): {shown}"
                    + (" (published)" if p.get("publish") else " (draft)"))
        return self.action


@dataclass
class Plan:
    name: str = "Untitled plan"
    site_url: str = ""
    steps: list[Step] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Plan":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        plan = cls(name=data.get("name", Path(path).stem), site_url=data.get("site_url", ""),
                   steps=[Step.from_dict(s) for s in data.get("steps", [])])
        plan.validate()
        return plan

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def to_dict(self) -> dict:
        return {"name": self.name, "site_url": self.site_url, "steps": [s.to_dict() for s in self.steps]}

    def validate(self) -> None:
        for i, s in enumerate(self.steps, 1):
            try:
                if s.action in AGENDA_ACTIONS:
                    AgendaFilter.from_dict(s.params.get("filter"))
                if s.action in ITEM_ACTIONS:
                    ItemFilter.from_dict(s.params.get("filter"))
                if s.action == "create_categories" and not s.params.get("names"):
                    raise ValueError("needs names")
            except Exception as e:
                raise ValueError(f"Step {i} ({s.action}): {e}") from e


class Runner:
    def __init__(self, site: AgendaCenterSite, reporter: Reporter, live: bool) -> None:
        self.site = site
        self.r = reporter
        self.live = live
        self.cancel = site.b.cancel

    # ------------------------------------------------------------------ main
    def run(self, plan: Plan) -> None:
        plan.validate()
        self.r.info(("LIVE RUN" if self.live else "DRY RUN (nothing will change)") + f": {plan.name}")
        forget = getattr(self.site, "forget_view_options", None)
        if forget:
            forget()
        for i, step in enumerate(plan.steps, 1):
            self.cancel.check()
            label = f"{i}/{len(plan.steps)} {step.action}"
            self.r.info(f"=== Step {label}: {step.describe()}")
            getattr(self, "_" + step.action)(label, step.params)
        self.r.info(f"Finished. {self.r.summary()}")

    # ------------------------------------------------------------- helpers
    def _res(self, step: str, action: str, ttype: str, **kw) -> None:
        self.r.result(ActionResult(step=step, action=action, target_type=ttype, **kw))

    def _agenda_res(self, step: str, action: str, a: Agenda, status: str, detail: str = "") -> None:
        self._res(step, action, "agenda", category=a.category_name, target_id=str(a.id),
                  meeting_date=a.meeting_date.isoformat() if a.meeting_date else "",
                  description=a.description, status=status, detail=detail)

    def _select(self, params: dict) -> list[Agenda]:
        flt = AgendaFilter.from_dict(params.get("filter"))
        unknown = flt.unknown_categories(self.site.categories(refresh=True))
        if unknown:
            self.r.warn("These categories are not on the site and were ignored: " + ", ".join(unknown))
        found = select_agendas(self.site, flt, progress=self.r.progress, cancel=self.cancel)
        self.r.info(f"{len(found)} agenda(s) match.")
        return found

    def _by_category(self, agendas: list[Agenda]) -> dict[int, list[Agenda]]:
        out: dict[int, list[Agenda]] = {}
        for a in agendas:
            out.setdefault(a.category_id, []).append(a)
        return out

    def _category(self, cid: int) -> Category:
        return next(c for c in self.site.categories() if c.id == cid)

    def _bulk(self, label: str, action: str, agendas: list[Agenda], undo_kind: Optional[str] = None) -> set[int]:
        """Run a bulk action category by category. Returns IDs that succeeded."""
        ok: set[int] = set()
        failures = 0
        groups = self._by_category(agendas)
        for n, (cid, group) in enumerate(groups.items(), 1):
            self.cancel.check()
            cat = self._category(cid)
            self.r.progress(n - 1, len(groups), f"{action} in {cat.name}")
            self.r.info(f"{cat.name}: {action} {len(group)} agenda(s)")
            try:
                self.site.ensure_view_options(cat)

                def batch_done(ids, cat=cat):
                    if undo_kind:
                        for i in ids:
                            self.r.record_undo({"undo": undo_kind, "category_id": cat.id,
                                                "category": cat.name, "agenda_id": i})
                results = self.site.bulk_agenda_action(cat, [a.id for a in group], action, batch_done)
            except Exception as e:  # keep going with other categories
                self.r.error(f"{cat.name}: {e}")
                results = {a.id: f"error: {e}" for a in group}
            cat_failed = False
            for a in group:
                status = results.get(a.id, "not processed")
                if status == "ok":
                    ok.add(a.id)
                    self._agenda_res(label, action, a, "ok")
                else:
                    cat_failed = True
                    self._agenda_res(label, action, a, "error", status)
            failures = failures + 1 if cat_failed else 0
            if failures >= MAX_CONSECUTIVE_FAILURES:
                self.r.error(f"{failures} categories in a row had errors; stopping this step.")
                break
        self.r.progress(len(groups), len(groups), f"{action} done")
        return ok

    # ----------------------------------------------------------- agenda steps
    def _scan_agendas(self, label: str, params: dict) -> None:
        for a in self._select(params):
            det = a.details
            self._agenda_res(label, "scan", a, "found",
                             f"{a.status}; updated {a.updated or '?'}; minutes: {'yes' if a.has_minutes else 'no'}"
                             + (f"; kind: {det.kind}" if det else ""))

    def _unpublish_agendas(self, label: str, params: dict) -> None:
        targets = [a for a in self._select(params) if a.published]
        if not self.live:
            for a in targets:
                self._agenda_res(label, "unpublish", a, "dry-run", "would unpublish")
            return
        self._bulk(label, "unpublish", targets, undo_kind="republish")

    def _publish_agendas(self, label: str, params: dict) -> None:
        targets = [a for a in self._select(params) if not a.published]
        if not self.live:
            for a in targets:
                self._agenda_res(label, "publish", a, "dry-run", "would publish (Notify Me: Don't Send)")
            return
        self._bulk(label, "publish", targets, undo_kind="unpublish")

    def _delete_agendas(self, label: str, params: dict) -> None:
        targets = self._select(params)
        unpublish_first = params.get("unpublish_first", True)
        published = [a for a in targets if a.published]
        if published and not unpublish_first:
            self.r.warn(f"{len(published)} matching agenda(s) are published and unpublish_first is off; "
                        "they will be skipped.")
            for a in published:
                self._agenda_res(label, "delete", a, "skipped", "published (unpublish_first is off)")
            targets = [a for a in targets if not a.published]
        if not self.live:
            for a in targets:
                self._agenda_res(label, "delete", a, "dry-run",
                                 "would unpublish, then delete" if a.published else "would delete")
            return
        if published and unpublish_first:
            done = self._bulk(label, "unpublish", published)
            targets = [a for a in targets if not a.published or a.id in done]
        self._bulk(label, "delete", targets)

    # ------------------------------------------------------------- item steps
    def _item_res(self, step: str, action: str, it: Item, status: str, detail: str = "") -> None:
        self._res(step, action, "default item" if it.default else "item", category=it.category_name,
                  target_id=str(it.id), meeting_date=it.meeting_date.isoformat() if it.meeting_date else "",
                  description=it.name, status=status, detail=detail)

    def _select_items(self, params: dict) -> list[Item]:
        flt = ItemFilter.from_dict(params.get("filter"))
        unknown = flt.unknown_categories(self.site.categories(refresh=True))
        if unknown:
            self.r.warn("These categories are not on the site and were ignored: " + ", ".join(unknown))
        found = select_items(self.site, flt, progress=self.r.progress, cancel=self.cancel)
        self.r.info(f"{len(found)} item(s) match.")
        return found

    def _scan_items(self, label: str, params: dict) -> None:
        for it in self._select_items(params):
            self._item_res(label, "scan", it, "found", it.status + (f"; in folder {it.folder_id}" if it.folder_id else ""))

    def _approve_items(self, label: str, params: dict) -> None:
        targets = [it for it in self._select_items(params) if it.status != "approved"]
        if not self.live:
            for it in targets:
                self._item_res(label, "approve", it, "dry-run", f"would approve (now {it.status})")
            return
        self._item_actions(label, "approve", targets)

    def _delete_items(self, label: str, params: dict) -> None:
        targets = self._select_items(params)
        if not self.live:
            for it in targets:
                self._item_res(label, "delete", it, "dry-run", "would delete")
            return
        self._item_actions(label, "delete", targets)

    def _item_actions(self, label: str, action: str, items: list[Item]) -> set[int]:
        """approve | delete items category by category: Items-tab items in bulk,
        default items one by one (the site has no bulk actions for them)."""
        ok: set[int] = set()
        failures = 0
        groups: dict[int, list[Item]] = {}
        for it in items:
            groups.setdefault(it.category_id, []).append(it)
        for n, (cid, group) in enumerate(groups.items(), 1):
            self.cancel.check()
            cat = self._category(cid)
            self.r.progress(n - 1, len(groups), f"{action} items in {cat.name}")
            self.r.info(f"{cat.name}: {action} {len(group)} item(s)")
            results: dict[int, str] = {}
            try:
                self.site.ensure_view_options(cat)
                regular = [it.id for it in group if not it.default]
                if regular:
                    results.update(self.site.bulk_item_action(cat, regular, action))
            except Exception as e:
                self.r.error(f"{cat.name}: {e}")
                results.update({it.id: f"error: {e}" for it in group if not it.default and it.id not in results})
            defaults = [x for x in group if x.default]
            if action == "delete" and len(defaults) > 1:
                # All default items in one request, then one read; the rest one by one.
                try:
                    self.site.fast_item_delete(cat, [x.id for x in defaults], default=True)
                    for x in defaults:
                        results[x.id] = "ok"
                except Exception as e:
                    self.r.warn(f"{cat.name}: default item delete in one request didn't work ({e}); "
                                "going one by one.")
            for it in (x for x in defaults if x.id not in results):
                self.cancel.check()
                try:
                    results[it.id] = self.site.item_action(cat, it, action)
                except Exception as e:
                    results[it.id] = f"error: {e}"
            cat_failed = False
            for it in group:
                st = results.get(it.id, "not processed")
                if st == "ok":
                    ok.add(it.id)
                    self._item_res(label, action, it, "ok")
                else:
                    cat_failed = True
                    self._item_res(label, action, it, "error", st)
            failures = failures + 1 if cat_failed else 0
            if failures >= MAX_CONSECUTIVE_FAILURES:
                self.r.error(f"{failures} categories in a row had errors; stopping this step.")
                break
        self.r.progress(len(groups), len(groups), f"{action} items done")
        return ok

    # ---------------------------------------------------------- category steps
    @staticmethod
    def _contents_summary(contents: dict[str, list]) -> str:
        names = {"agendas": "agenda(s)", "items": "item(s)", "default_items": "default item(s)",
                 "folders": "item folder(s)", "workflows": "workflow(s)"}
        return ", ".join(f"{len(v)} {names[k]}" for k, v in contents.items() if v)

    def _purge_category(self, label: str, cat: Category, contents: dict[str, list]) -> str:
        """Delete everything in a category that blocks deleting it. Agendas go
        first (that also deletes the items placed on them), then the unused
        items and default items, then the emptied folders, then workflows.
        Returns what is still left ("" = empty)."""
        # Everything comes from the one reading of the category (contents);
        # nothing is read again: the category delete itself says if anything is left.
        ags = contents["agendas"]
        if ags:
            pub = [a for a in ags if a.published]
            done = self._bulk(label, "unpublish", pub) if pub else set()
            self._bulk(label, "delete", [a for a in ags if not a.published or a.id in done])
        items = contents["items"] + contents["default_items"]
        if items:
            self._item_actions(label, "delete", items)
        for f in contents["folders"]:
            self.cancel.check()
            try:
                st = self.site.delete_folder(cat, f)
            except Exception as e:
                st = f"error: {e}"
            self._res(label, "delete", "item folder", category=cat.name, target_id=str(f.id), description=f.name,
                      status="ok" if st == "ok" else "error", detail="" if st == "ok" else st)
        for w in contents["workflows"]:
            self.cancel.check()
            try:
                st = self.site.delete_workflow(cat, w)
            except Exception as e:
                st = f"error: {e}"
            self._res(label, "delete", "workflow", category=cat.name, target_id=str(w.id), description=w.name,
                      status="ok" if st == "ok" else "error", detail="" if st == "ok" else st)
        return ""

    def _pick_categories(self, params: dict) -> list[Category]:
        cats = self.site.categories(refresh=True)
        names = params.get("categories") or []
        if not names:
            if not params.get("all"):
                raise ValueError("Give category names, or set \"all\": true to mean every category.")
            return cats
        picked = []
        for n in names:
            c = self.site.find_category(n)
            if c is None:
                self.r.warn(f"Category not found: {n}")
                self._res("", "find", "category", category=n, status="error", detail="not found")
            else:
                picked.append(c)
        return picked

    def _publish_categories(self, label: str, params: dict) -> None:
        self._category_step(label, params, "publish")

    def _unpublish_categories(self, label: str, params: dict) -> None:
        self._category_step(label, params, "unpublish")

    def _delete_categories(self, label: str, params: dict) -> None:
        self._category_step(label, params, "delete")

    def _category_step(self, label: str, params: dict, action: str) -> None:
        self.category_action(label, self._pick_categories(params), action, delete_contents_first(params))

    def category_action(self, label: str, cats: list[Category], action: str, purge: bool = False) -> None:
        """publish | unpublish | delete these categories. purge: for delete,
        first delete everything inside (agendas, items, folders, workflows)."""
        for cat in cats:
            self.cancel.check()
            want_pub = action == "publish"
            if action != "delete" and cat.published == want_pub:
                self._res(label, action, "category", category=cat.name, target_id=str(cat.id),
                          status="skipped", detail=f"already {action}ed")
                continue
            contents = self.site.contents(cat) if action == "delete" else {}
            summary = self._contents_summary(contents) if contents else ""
            blocked = (f"can't delete: it still has {summary}. Tick \"first delete everything inside them\" "
                       "(delete_contents_first) to delete those too.") if summary and not purge else ""
            if not self.live:
                if blocked:
                    self._res(label, action, "category", category=cat.name, target_id=str(cat.id),
                              status="skipped", detail=blocked)
                    continue
                detail = f"would {action}"
                if action == "delete":
                    if cat.published:
                        detail += " (unpublish it first)"
                    if summary:
                        detail += f"; first delete its {summary}"
                self._res(label, action, "category", category=cat.name, target_id=str(cat.id),
                          status="dry-run", detail=detail)
                continue
            if action == "delete":
                if summary:
                    if blocked:
                        self._res(label, action, "category", category=cat.name, target_id=str(cat.id),
                                  status="skipped", detail=blocked)
                        continue
                    left = self._purge_category(label, cat, contents)
                    if left:
                        self._res(label, action, "category", category=cat.name, target_id=str(cat.id),
                                  status="error", detail=f"not deleted: it still has {left}")
                        continue
                if cat.published:
                    st = self.site.category_action(cat, "unpublish")
                    if st != "ok":
                        self._res(label, action, "category", category=cat.name, target_id=str(cat.id),
                                  status="error", detail=f"unpublish failed: {st}")
                        continue
            st = self.site.category_action(cat, action)
            if st != "ok" and action == "delete" and purge:
                left = self._contents_summary(self.site.contents(cat))
                if left:
                    self.r.info(f"{cat.name}: still has {left}; clearing it again.")
                    self._purge_category(label, cat, self.site.contents(cat))
                    st = self.site.category_action(cat, action)
            if st == "ok" and action == "unpublish":
                self.r.record_undo({"undo": "republish_category", "category_id": cat.id, "category": cat.name})
            self._res(label, action, "category", category=cat.name, target_id=str(cat.id),
                      status="ok" if st == "ok" else "error", detail="" if st == "ok" else st)

    def _create_categories(self, label: str, params: dict) -> None:
        publish = bool(params.get("publish"))
        for i, name in enumerate(params.get("names") or []):
            self.cancel.check()
            existing = self.site.find_category(name, refresh=(i == 0))
            if existing:
                self._res(label, "create", "category", category=name, target_id=str(existing.id),
                          status="skipped", detail="already exists")
                continue
            if not self.live:
                self._res(label, "create", "category", category=name, status="dry-run",
                          detail="would create" + (" and publish" if publish else " as draft"))
                continue
            cat, st = self.site.create_category(name, publish)
            self._res(label, "create", "category", category=name, target_id=str(cat.id) if cat else "",
                      status="ok" if st == "ok" else "error", detail="" if st == "ok" else st)
        if self.live and params.get("names"):
            # The one check worth making before an import: Content Import only
            # sees published categories that exist. One read of the list; any
            # that's missing is made through the page, any draft is published.
            now = {c.name.strip().lower(): c for c in self.site.categories(refresh=True)}
            for name in params["names"]:
                c = now.get(name.strip().lower())
                if c is None:
                    self.r.warn(f"Category '{name}' isn't on the site after creating it; making it through the page.")
                    c, st = self.site.create_category(name, publish, direct=False)
                    self._res(label, "create", "category", category=name, target_id=str(c.id) if c else "",
                              status="ok" if c else "error", detail="made through the page" if c else st)
                elif publish and not c.published:
                    st = self.site.category_action(c, "publish")
                    self._res(label, "publish", "category", category=name, target_id=str(c.id),
                              status="ok" if st == "ok" else "error",
                              detail="it was still a draft" if st == "ok" else st)

def undo_plan(path: Path) -> "Plan":
    """A plan that reverses what a live run recorded in its undo.json."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    republish = [e["agenda_id"] for e in entries if e.get("undo") == "republish"]
    unpublish = [e["agenda_id"] for e in entries if e.get("undo") == "unpublish"]
    created = [e["agenda_id"] for e in entries if e.get("undo") == "delete_created"]
    cats = [e["category"] for e in entries if e.get("undo") == "republish_category"]
    steps = []
    if cats:
        steps.append(Step("publish_categories", {"categories": cats}))
    if republish:
        steps.append(Step("publish_agendas", {"filter": {"agenda_ids": republish}}))
    if unpublish:
        steps.append(Step("unpublish_agendas", {"filter": {"agenda_ids": unpublish}}))
    if created:
        steps.append(Step("delete_agendas", {"filter": {"agenda_ids": created}, "unpublish_first": True}))
    return Plan(name=f"Undo {Path(path).parent.name}", steps=steps)
