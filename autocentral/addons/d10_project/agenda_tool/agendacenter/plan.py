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

from .filters import AgendaFilter, ItemFilter, _d, select_agendas, select_items
from .importdata import ImportOptions, check_workbook, files_search, find_file, search_dirs_for, is_duplicate, load_meetings
from .models import ALL_KINDS, Agenda, Category, Item, Meeting
from ..reporting import ActionResult, Reporter
from .site import AgendaCenterSite
from .tasks import find_agenda

AGENDA_ACTIONS = ("scan_agendas", "unpublish_agendas", "delete_agendas", "publish_agendas")
ITEM_ACTIONS = ("scan_items", "approve_items", "delete_items")
CATEGORY_ACTIONS = ("publish_categories", "unpublish_categories", "delete_categories")
ALL_ACTIONS = AGENDA_ACTIONS + ITEM_ACTIONS + CATEGORY_ACTIONS + ("create_categories", "upload_agendas",
                                                                  "post_import_tasks")
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
        if self.action == "post_import_tasks":
            from .tasks import KIND_LABELS
            kinds = p.get("kinds") or list(KIND_LABELS)
            return (f"post-import tasks from {Path(p.get('tasks_file') or '').name}: "
                    + ", ".join(KIND_LABELS.get(k, k) for k in kinds)
                    + (", then publish" if p.get("publish", True) else "")
                    + (" (download missing files from the old site)" if p.get("download_missing") else ""))
        if self.action == "upload_agendas":
            src = Path(p.get("workbook") or "").name or "(no workbook)"
            return (f"upload_agendas from {src}" + (f" + {Path(p['json_path']).name}" if p.get("json_path") else "")
                    + (" and publish" if p.get("publish") else " as drafts"))
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
                if s.action == "upload_agendas":
                    if not s.params.get("workbook") and not s.params.get("json_path"):
                        raise ValueError("needs a workbook and/or json_path")
                    if s.params.get("workbook"):
                        check_workbook(Path(s.params["workbook"]))
                    if s.params.get("json_path") and not Path(s.params["json_path"]).is_file():
                        raise ValueError(f"JSON file not found: {s.params['json_path']}")
                    for k in s.params.get("kinds") or []:
                        if k not in ALL_KINDS:
                            raise ValueError(f"unknown kind {k!r}")
                if s.action == "post_import_tasks":
                    if not Path(s.params.get("tasks_file") or "").is_file():
                        raise ValueError(f"tasks file not found: {s.params.get('tasks_file')}")
                    if s.params.get("files_dir") and not Path(s.params["files_dir"]).is_dir():
                        raise ValueError(f"files folder not found: {s.params['files_dir']}")
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

    # ------------------------------------------------------------- uploading
    def _upload_agendas(self, label: str, params: dict) -> None:
        opts = ImportOptions.from_dict(params)
        meetings, warnings = load_meetings(opts)
        for w in warnings:
            self.r.warn(w)
        meetings = self._filter_meetings(meetings, params)
        self.r.info(f"{len(meetings)} agenda(s) to consider from the import files.")
        self.upload_meetings(label, meetings, self.resolver(opts, meetings, params), params)

    def resolver(self, opts: ImportOptions, meetings: list[Meeting], params: dict):
        """file name -> local path. Looks in the files folder (and the JSON's
        folders); with download_missing, fetches a file it can't find from
        where the old (D10) site served it, into <files folder>/downloaded."""
        files_dir, deep = files_search(opts)
        search_dirs = search_dirs_for(opts, files_dir)
        if opts.json_path:
            jdir = Path(opts.json_path).parent
            search_dirs += [jdir / t[len("files in: "):] for m in meetings for t in m.json_tasks
                            if t.startswith("files in: ")]
        urls = {n.lower(): u for m in meetings for n, u in m.file_urls.items()}
        download_dir = (files_dir or Path.cwd()) / "downloaded"
        if params.get("download_missing"):
            search_dirs.append(download_dir)

        def resolve(name: str) -> Path:
            p = find_file(name, search_dirs, deep=deep)
            if p is not None:
                return p
            url = urls.get(name.lower())
            if params.get("download_missing") and url:
                return download_file(url, download_dir / name, self.r)
            raise FileNotFoundError(name)
        return resolve

    def upload_meetings(self, label: str, meetings: list[Meeting], resolve, params: dict) -> None:
        """Create these agendas (simple, minutes-only, complex), category by
        category. params: publish, on_missing_category (skip|create),
        on_duplicate (skip|upload)."""
        on_missing = params.get("on_missing_category", "skip")
        on_duplicate = params.get("on_duplicate", "skip")
        publish = bool(params.get("publish"))
        by_cat: dict[str, list[Meeting]] = {}
        for m in meetings:
            by_cat.setdefault(m.category, []).append(m)

        done_total, total = 0, len(meetings)
        for cat_name, group in by_cat.items():
            self.cancel.check()
            cat = self.site.find_category(cat_name)
            if cat is None:
                if on_missing != "create":
                    for m in group:
                        self._meeting_res(label, m, "skipped", f"category '{cat_name}' is not on the site "
                                          "(on_missing_category=skip)")
                    done_total += len(group)
                    continue
                if not self.live:
                    self._res(label, "create", "category", category=cat_name, status="dry-run",
                              detail="would create (missing)" + (" and publish" if publish else ""))
                else:
                    cat, st = self.site.create_category(cat_name, publish)
                    self._res(label, "create", "category", category=cat_name,
                              target_id=str(cat.id) if cat else "", status="ok" if cat else "error", detail=st)
                    if cat is None:
                        for m in group:
                            self._meeting_res(label, m, "error", "category could not be created")
                        continue

            existing = self.site.agendas(cat) if cat else []
            made: dict = {}
            created: list[int] = []
            resumed: list[int] = []           # drafts an earlier, stopped run left
            needs_minutes: list[tuple[int, Meeting]] = []
            failures = 0
            for m in group:
                self.cancel.check()
                self.site.b.handle_session_timeout()
                done_total += 1
                self.r.progress(done_total, total, f"{cat_name} {m.meeting_date}")
                if m.problems:
                    self._meeting_res(label, m, "skipped", "; ".join(m.problems))
                    continue
                if cat is not None and on_duplicate == "skip":
                    dup = next((a for a in existing if a.meeting_date == m.meeting_date
                                and is_duplicate(m, a, self.site.details)), None)
                    if dup is not None:
                        # A run stopped between creating and publishing leaves a
                        # draft (seen live 2026-11-17): finish it on the rerun.
                        unfinished = self.live and publish and not dup.published
                        late = self.live and m.later_minutes and not dup.has_minutes
                        if unfinished or late:
                            resumed.append(dup.id)
                            if late:
                                needs_minutes.append((dup.id, m))
                        self._meeting_res(label, m, "skipped", f"already on the site as agenda {dup.id}"
                                          + ("; finishing its publish/minutes" if unfinished or late else ""))
                        continue
                if not self.live:
                    self._meeting_res(label, m, "dry-run", f"would create {m.kind} agenda"
                                      + (" and publish" if publish else " as draft"))
                    continue
                try:
                    new_id, st = (self.site.direct_create_agenda(cat, m, resolve) if m.kind != "complex"
                                  else (None, "complex"))
                    # The page only when nothing was sent (complex, files kept in the cloud, no form):
                    # after a send, trying again could make the agenda twice.
                    if new_id is None and st in ("complex", "cloud", "no Add Agenda form"):
                        new_id, st = self.site.create_agenda(cat, m, resolve)
                except Exception as e:
                    new_id, st = None, f"error: {e}"
                if new_id is not None:
                    created.append(new_id)
                    made[new_id] = m
                    self.r.record_undo({"undo": "delete_created", "category_id": cat.id,
                                        "category": cat.name, "agenda_id": new_id})
                    if m.later_minutes and st == "ok":
                        needs_minutes.append((new_id, m))
                        st_detail = "created; minutes are added after it is published"
                    else:
                        st_detail = st
                else:
                    st_detail = st
                self._meeting_res(label, m, "ok" if st == "ok" else "error", st_detail, agenda_id=new_id)
                failures = 0 if st == "ok" else failures + 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    self.r.error(f"{failures} uploads in a row failed in {cat_name}; moving on.")
                    break

            if not self.live or not (created or resumed):
                continue
            # Minutes can only be added to a published agenda, so agendas that get
            # minutes later are published even on a draft upload, then unpublished.
            to_publish = set(created + resumed) if publish else {i for i, _ in needs_minutes}
            if to_publish:
                self.r.info(f"{cat_name}: publishing {len(to_publish)} new agenda(s) (Notify Me: Don't Send)")
                # The new agendas' IDs came back when they were made: no need to read the list again.
                fresh = [Agenda(i, cat.id, cat.name, made[i].meeting_date if i in made else None, None,
                                made[i].description if i in made else "", False, "draft", False)
                         for i in to_publish if i in made] + \
                        [a for a in existing if a.id in to_publish and not a.published]
                published_ok = self._bulk(label, "publish", fresh) | \
                    {a.id for a in existing if a.id in to_publish and a.published}
            else:
                published_ok = set()
            for agenda_id, m in needs_minutes:
                self.cancel.check()
                if agenda_id not in published_ok:
                    self._meeting_res(label, m, "error", "minutes not added: the agenda could not be published",
                                      agenda_id=agenda_id)
                    continue
                try:
                    path = resolve(m.later_minutes)
                    # Sent straight to the site (Save and Publish, as after Don't Send); the page only when
                    # the minutes stay drafts or the site keeps files in the cloud.
                    st = self.site.direct_minutes(cat, agenda_id, path) if publish else "cloud"
                    if st == "cloud":
                        st = self.site.add_minutes(cat, agenda_id, path, publish=publish)
                except Exception as e:
                    st = f"error: {e}"
                self._res(label, "add_minutes", "minutes", category=cat.name, target_id=str(agenda_id),
                          meeting_date=m.meeting_date.isoformat(), description=m.later_minutes,
                          status="ok" if st == "ok" else "error", detail="" if st == "ok" else st)
            if not publish and needs_minutes:
                back = [a for a in self.site.agendas(cat) if a.id in {i for i, _ in needs_minutes} and a.published]
                if back:
                    self.r.info(f"{cat_name}: returning {len(back)} agenda(s) to draft after adding minutes")
                    self._bulk(label, "unpublish", back)

    def add_minutes_to_existing(self, label: str, targets: list[tuple[int, int, Meeting]], resolve,
                                publish: bool = True) -> None:
        """Add the import's minutes to agendas already on the site:
        targets = [(category id, agenda id, meeting)]. The site only allows Add
        Minutes on a published agenda, so a draft agenda is published for it
        (Notify Me: Don't Send) and put back to draft afterwards."""
        groups: dict[int, list[tuple[int, Meeting]]] = {}
        for cid, aid, m in targets:
            groups.setdefault(cid, []).append((aid, m))
        for cid, group in groups.items():
            self.cancel.check()
            cat = self._category(cid)
            by_id = {a.id: a for a in self.site.agendas(cat)}
            drafts = [by_id[aid] for aid, _ in group if aid in by_id and not by_id[aid].published]
            if not self.live:
                for aid, m in group:
                    fname = m.minutes_file or m.attach_minutes_file
                    note = " (publishes the draft agenda for it, then puts it back)" if aid in {a.id for a in drafts} else ""
                    self._res(label, "add_minutes", "minutes", category=cat.name, target_id=str(aid),
                              meeting_date=m.meeting_date.isoformat(), description=fname, status="dry-run",
                              detail="would add minutes" + note)
                continue
            published_now = self._bulk(label, "publish", drafts) if drafts else set()
            for aid, m in group:
                self.cancel.check()
                fname = m.minutes_file or m.attach_minutes_file
                if aid not in by_id:
                    st = "error: the agenda is no longer on the site"
                elif not by_id[aid].published and aid not in published_now:
                    st = "error: the agenda could not be published, so minutes can't be added"
                else:
                    try:
                        st = self.site.add_minutes(cat, aid, resolve(fname), publish=publish)
                    except Exception as e:
                        st = f"error: {e}"
                self._res(label, "add_minutes", "minutes", category=cat.name, target_id=str(aid),
                          meeting_date=m.meeting_date.isoformat(), description=fname,
                          status="ok" if st == "ok" else "error", detail="" if st == "ok" else st)
            if published_now:
                back = [a for a in self.site.agendas(cat) if a.id in published_now and a.published]
                if back:
                    self.r.info(f"{cat.name}: returning {len(back)} agenda(s) to draft after adding minutes")
                    self._bulk(label, "unpublish", back)

    # ------------------------------------------------- post-import tasks
    def _post_import_tasks(self, label: str, params: dict) -> None:
        """Finish what the migration tool's importer can't, from the tasks
        JSON alone (see tasks.py): first the meetings it couldn't create,
        then each agenda one at a time, finished before the next: its
        minutes, its video links and Save (builds an item-based agenda's
        HTML page) in one visit to the edit page, then publish. Each
        category's agenda list is read once for the whole step."""
        from .tasks import CREATE, KINDS, MEDIA, MINUTES, SAVE, load_tasks
        tasks_file = Path(params["tasks_file"])
        tasks, warnings = load_tasks(tasks_file)
        for w in warnings:
            self.r.warn(w)
        tasks = [t for t in tasks if t.category]
        kinds = [k for k in KINDS if k in (params.get("kinds") or KINDS)]
        resolve, can_find = self._tasks_resolver(tasks_file, tasks, params)
        # The imported agendas first (matched before any same-date meeting
        # is added), then the meetings the importer couldn't make.
        if MINUTES in kinds or SAVE in kinds or MEDIA in kinds:
            self.cancel.check()
            self._pit_agendas(label, tasks, kinds, resolve, can_find, params)
        if CREATE in kinds:
            todo = [t for t in tasks if CREATE in t.tasks]
            self.r.info(f"{len(todo)} meeting(s) to create.")
            self._pit_create(label, todo, resolve, can_find, params)

    def _one(self, label: str, action: str, cat: Category, a: Agenda, undo_kind: Optional[str] = None) -> bool:
        """Publish or unpublish one agenda (Notify Me: Don't Send)."""
        def batch_done(ids):
            if undo_kind:
                for i in ids:
                    self.r.record_undo({"undo": undo_kind, "category_id": cat.id, "category": cat.name,
                                        "agenda_id": i})
        try:
            self.site.ensure_view_options(cat)
            st = self.site.bulk_agenda_action(cat, [a.id], action, batch_done,
                                              page_hint=a.list_page).get(a.id, "not processed")
        except Exception as e:
            st = f"error: {e}"
        self._agenda_res(label, action, a, "ok" if st == "ok" else "error", "" if st == "ok" else st)
        if st == "ok":
            a.published = action == "publish"
        return st == "ok"

    def _pit_parallel(self, label: str, groups: dict, tabs: int, resolve) -> tuple[dict, int]:
        """Drafts that need saving (and their minutes) done several categories
        at a time, one tab per category (parallel.py). Returns what's left
        for the usual way, and how many were done."""
        from .parallel import Job, finish
        jobs: dict = {}
        rest: dict = {}
        for cid, group in groups.items():
            cat = self._category(cid)
            for entry in group:
                t, a, minutes, save, links = entry
                if (save or links) and not a.published:
                    path = None
                    if minutes:
                        try:
                            path = resolve(t.minutes_file)
                        except Exception as e:
                            self._task_res(label, "add_minutes", t, "error", f"minutes file: {e}", str(a.id),
                                           files=t.minutes_file)
                    jobs.setdefault(cat.id, (cat, []))[1].append(Job(a.id, (t.video or None) if links else None,
                                                        (t.additional_video or None) if links else None,
                                                        save=True, minutes=path, ref=entry))
                else:
                    rest.setdefault(cid, []).append(entry)
        n = sum(len(v) for _, v in jobs.values())
        self.r.info(f"{n} agenda(s) in {len(jobs)} categories: finishing them {tabs} tabs at a time "
                    "(Save and Publish, then minutes; Notify Me: Don't Send).")
        done = finish(self.site, jobs, tabs=tabs, log=self.r.info, cancelled=lambda: self.cancel.cancelled)
        for j in done:
            t, a, minutes, save, links = j.ref
            if j.handed_back or not j.results:
                rest.setdefault(a.category_id, []).append(j.ref)
                continue
            action = "save + video_links" if save and links else "save" if save else "video_links"
            for what, st in j.results:
                if what == "save":
                    if st == "ok":
                        a.published = True
                        self.r.record_undo({"undo": "unpublish", "category_id": a.category_id,
                                            "category": a.category_name, "agenda_id": a.id})
                    self._task_res(label, action, t, "ok" if st == "ok" else "error", "" if st == "ok" else st,
                                   str(a.id))
                else:
                    if st == "ok":
                        a.has_minutes = True
                    self._task_res(label, "add_minutes", t, "ok" if st == "ok" else "error",
                                   "" if st == "ok" else st, str(a.id), files=t.minutes_file)
            if minutes and not any(w == "minutes" for w, _ in j.results) and a.published:
                rest.setdefault(a.category_id, []).append((t, a, minutes, False, False))
        return rest, n

    def _pit_agendas(self, label: str, tasks, kinds, resolve, can_find, params: dict) -> None:
        """Minutes, video links, saves and publishing, category by category,
        the fastest way that gives the same result (measured on training19):
          1. saves (and video links) sent straight to the site while the
             agendas are drafts, Save and Publish in the same request when
             the run publishes (Notify Me: Don't Send): builds item-based
             agendas' HTML page, no Amended / RSS questions, no Posted date
             to fix
          2. publish the rest in one request per category (Notify Me:
             Don't Send); minutes can only go on published agendas
          3. minutes sent straight to the site, Save and Publish with the
             same answer the page sends for Don't Send
        Anything unusual (an agenda already published, a site that keeps
        files in the cloud) goes the old way, through the page."""
        from .tasks import MEDIA, MINUTES, SAVE
        publish = params.get("publish", True)
        cache: dict = {}
        self.agenda_cache = cache            # read once here; "publish the rest" uses it too
        groups: dict[int, list] = {}
        todo = [t for t in tasks if (MINUTES in kinds and MINUTES in t.tasks) or (SAVE in kinds and SAVE in t.tasks)
                or (MEDIA in kinds and MEDIA in t.tasks and (t.video or t.additional_video))]
        self.r.info(f"{len(todo)} agenda(s) to finish.")
        for t in todo:
            self.cancel.check()
            if t.meeting_date is None:
                self._task_res(label, "agenda", t, "skipped", "no meeting date in the D10 file, so it wasn't "
                                                              "imported (see left-out.csv)")
                continue
            cat, agendas = self._site_agendas(t.category, cache)
            if cat is None:
                self._task_res(label, "agenda", t, "skipped", f"category '{t.category}' isn't on the site")
                continue
            a, why = find_agenda(t, agendas)
            if a is None:
                self._task_res(label, "agenda", t, "error", f"agenda not found: {why}")
                continue
            minutes = MINUTES in kinds and MINUTES in t.tasks
            if minutes and a.has_minutes:
                self._task_res(label, "add_minutes", t, "skipped", "the agenda already has minutes", str(a.id))
                minutes = False
            elif minutes and (not t.minutes_file or can_find(t.minutes_file) == "missing"):
                self._task_res(label, "add_minutes", t, "skipped",
                               f"minutes file not found: {t.minutes_file or '(none named)'}", str(a.id),
                               files=t.minutes_file)
                minutes = False
            save = SAVE in kinds and SAVE in t.tasks
            links = MEDIA in kinds and MEDIA in t.tasks and bool(t.video or t.additional_video)
            groups.setdefault(cat.id, []).append((t, a, minutes, save, links))

        done_n, total = 0, len(todo)
        tabs = int(params.get("tabs") or 1)
        if tabs > 1 and self.live and publish and groups:
            groups, done_n = self._pit_parallel(label, groups, tabs, resolve)
        for cid, group in groups.items():
            cat = self._category(cid)
            # 1. saves and video links
            for t, a, minutes, save, links in group:
                if not (save or links):
                    continue
                self.cancel.check()
                self.site.b.handle_session_timeout()     # the "still logged in?" prompt blocks requests
                done_n += 1
                if done_n % 25 == 0:
                    self.r.info(f"{done_n} of {total} agenda tasks done")
                self.r.progress(done_n, total, f"{cat.name}: saves")
                action = "save + video_links" if save and links else "save" if save else "video_links"
                extra = f"; {len(t.overflow)} more video link(s) have no field on Central" if t.overflow else ""
                video = (t.video or None) if links else None
                addl = (t.additional_video or None) if links else None
                if not self.live:
                    what = [w for w in (f"set Link to Media to {t.video}" if links and t.video else "",
                                        f"set Additional Video Link to {t.additional_video}" if links and t.additional_video else "",
                                        "save (builds the HTML agenda page)" if save else "") if w]
                    self._task_res(label, action, t, "dry-run", "would " + ", ".join(what) + extra, str(a.id))
                    continue
                try:
                    st = self.site.direct_save(cat, a.id, video, addl, publish=publish) if not a.published else "published"
                    if st == "ok" and publish:
                        a.published = True          # saved and published in the same request
                        self.r.record_undo({"undo": "unpublish", "category_id": cat.id, "category": cat.name,
                                            "agenda_id": a.id})
                    elif st == "published":
                        # Already published (an earlier run): nothing to do if its HTML page opens and its
                        # links are right; else save it straight (Posted date left as it is).
                        a.published = True
                        st = "already done" if self.site.already_saved(cat, a, video, addl) else                             self.site.direct_save(cat, a.id, video, addl, allow_published=True)
                except Exception as e:
                    st = f"error: {e}"
                if st in ("already set", "already done"):
                    self._task_res(label, action, t, "skipped", "already done (published, HTML page opens, links "
                                   "right)" + extra, str(a.id))
                else:
                    self._task_res(label, action, t, "ok" if st == "ok" else "error",
                                   ("" if st == "ok" else st) + extra, str(a.id))

            # 2. publish (all of them, or only those that need minutes when the run doesn't publish)
            need = [a for t, a, minutes, save, links in group if not a.published and (publish or minutes)]
            need = list({a.id: a for a in need}.values())
            published_for_minutes: set[int] = set()
            if need:
                if not self.live:
                    for a in need:
                        self._agenda_res(label, "publish", a, "dry-run", "would publish (Notify Me: Don't Send)"
                                         + ("" if publish else ", for its minutes, then back to draft"))
                        a.published = True
                else:
                    ok = self._bulk(label, "publish", need, undo_kind="unpublish" if publish else None)
                    for a in need:
                        if a.id in ok:
                            a.published = True
                            if not publish:
                                published_for_minutes.add(a.id)

            # 3. minutes
            for t, a, minutes, save, links in group:
                if not minutes:
                    continue
                self.cancel.check()
                self.site.b.handle_session_timeout()     # the "still logged in?" prompt blocks requests
                done_n += 1
                if done_n % 25 == 0:
                    self.r.info(f"{done_n} of {total} agenda tasks done")
                self.r.progress(done_n, total, f"{cat.name}: minutes")
                if not self.live:
                    self._task_res(label, "add_minutes", t, "dry-run", "would add minutes (Save and Publish, "
                                   "Notify Me: Don't Send)", str(a.id), files=t.minutes_file)
                    continue
                if not a.published:
                    st = "error: the agenda could not be published, so minutes can't be added"
                else:
                    try:
                        path = resolve(t.minutes_file)
                        st = self.site.direct_minutes(cat, a.id, path) if publish else "cloud"
                        if st == "cloud":
                            st = self.site.add_minutes(cat, a.id, path, publish=publish)
                    except Exception as e:
                        st = f"error: {e}"
                if st == "ok":
                    a.has_minutes = True
                self._task_res(label, "add_minutes", t, "ok" if st == "ok" else "error", "" if st == "ok" else st,
                               str(a.id), files=t.minutes_file)

            if published_for_minutes:
                back = [a for t, a, *_ in group if a.id in published_for_minutes]
                back = list({a.id: a for a in back}.values())
                undone = self._bulk(label, "unpublish", back)
                for a in back:
                    if a.id in undone:
                        a.published = False
        if total:
            self.r.progress(total, total, "agendas done")

    def _tasks_resolver(self, tasks_file: Path, tasks, params: dict):
        """Files are looked for in the files folder, the tasks file's folder,
        their <site>-agenda-center-attach-after-import folders and anywhere
        below them (a name found once). With download_missing, a file that
        isn't there is fetched from the old site into <files folder>/downloaded."""
        from .tasks import NO_AGENDA_TEMPLATE, no_agenda_template_pdf, to_meeting
        base = Path(params["files_dir"]) if params.get("files_dir") else tasks_file.parent
        folders = {t.local_folder for t in tasks if t.local_folder} | \
                  {(t.manual or {}).get("localFolder") for t in tasks if t.manual and t.manual.get("localFolder")}
        dirs = [base] + [base / f for f in folders] + [tasks_file.parent] + [tasks_file.parent / f for f in folders]
        download_dir = base / "downloaded"
        dirs.append(download_dir)
        urls: dict[str, str] = {}
        for t in tasks:
            if t.minutes_file and t.minutes_url:
                urls[t.minutes_file.lower()] = t.minutes_url
            m = to_meeting(t) if t.manual else None
            if m:
                urls.update({n.lower(): u for n, u in m.file_urls.items()})

        def local(name: str) -> Optional[Path]:
            return find_file(name, [d for d in dirs if d.exists()], deep=True)

        def can_find(name: str) -> str:
            """'' if the file is there; else what will happen."""
            if name == NO_AGENDA_TEMPLATE or local(name) is not None:
                return ""
            if params.get("download_missing") and urls.get(name.lower()):
                return "download"
            return "missing"

        def resolve(name: str) -> Path:
            p = local(name)
            if p is not None:
                return p
            if name == NO_AGENDA_TEMPLATE:
                download_dir.mkdir(parents=True, exist_ok=True)
                (download_dir / name).write_bytes(no_agenda_template_pdf())
                return download_dir / name
            url = urls.get(name.lower())
            if params.get("download_missing") and url:
                return download_file(url, download_dir / name, self.r)
            raise FileNotFoundError(name)
        return resolve, can_find

    def _task_res(self, label: str, action: str, t, status: str, detail: str = "", target_id: str = "",
                  files: str = "") -> None:
        self._res(label, action, "agenda", category=t.category, target_id=target_id,
                  meeting_date=t.meeting_date.isoformat() if t.meeting_date else "", description=t.description,
                  status=status, detail=detail, files=files)

    def _site_agendas(self, name: str, cache: dict):
        """(category, its agendas) by name, read once per step."""
        key = name.strip().lower()
        if key not in cache:
            cat = self.site.find_category(name, refresh=not cache)
            if cat is not None:
                self.site.ensure_view_options(cat)
            cache[key] = (cat, self.site.agendas(cat) if cat is not None else [])
        return cache[key]

    def _pit_create(self, label: str, todo, resolve, can_find, params: dict) -> None:
        from .tasks import to_meeting
        meetings = []
        for t in todo:
            m = to_meeting(t)
            if m is None:
                self._task_res(label, "create", t, "skipped",
                               "no meeting date" if t.manual else "the tasks file has no details for it (use the .json)")
                continue
            missing = [f for f in m.all_files() if can_find(f) == "missing"]
            if missing:
                m.problems.append("missing file(s): " + ", ".join(missing))
            elif any(can_find(f) == "download" for f in m.all_files()):
                m.notes.append("some files will be downloaded from the old site")
            meetings.append(m)
        if meetings:
            self.upload_meetings(label, meetings, resolve, {
                "publish": params.get("publish", True), "on_duplicate": "skip",
                "on_missing_category": "create" if params.get("create_missing_categories") else "skip"})

    def _filter_meetings(self, meetings: list[Meeting], params: dict) -> list[Meeting]:
        cats = {c.strip().lower() for c in params.get("categories") or []}
        kinds = set(params.get("kinds") or [])
        lo, hi = _d(params.get("meeting_from")), _d(params.get("meeting_to"))
        out = [m for m in meetings
               if (not cats or m.category.strip().lower() in cats)
               and (not kinds or m.kind in kinds)
               and (lo is None or m.meeting_date >= lo) and (hi is None or m.meeting_date <= hi)]
        limit = params.get("limit")
        return out[:limit] if limit else out

    def _meeting_res(self, label: str, m: Meeting, status: str, detail: str, agenda_id: Optional[int] = None) -> None:
        files = " | ".join(x for x in (
            f"agenda: {m.agenda_file}" if m.agenda_file else "",
            f"minutes: {m.minutes_file}" if m.minutes_file else "",
            *(f"item '{it.name}': {', '.join(it.files)}" for it in m.items),
            f"minutes (added after publish): {m.attach_minutes_file}" if m.attach_minutes_file else "") if x)
        self._res(label, "upload", "agenda", category=m.category, target_id=str(agenda_id or ""),
                  meeting_date=m.meeting_date.isoformat(), description=m.description, status=status, files=files,
                  detail=f"[{m.kind}; {m.source} row {m.source_row}] {detail}" if m.source == "workbook"
                  else f"[{m.kind}; json] {detail}")


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


def download_file(url: str, dest: Path, r: Reporter) -> Path:
    """Fetch one file from the old site (public URL) to dest. Only used when
    the run asks for download_missing; never touches the Central site."""
    import urllib.request
    from urllib.parse import urlparse
    if urlparse(url).scheme not in ("http", "https"):
        raise FileNotFoundError(f"{dest.name}: not an http(s) address: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "CentralAutomation file fetch"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)
    r.info(f"Downloaded {dest.name} from the old site.")
    return dest
