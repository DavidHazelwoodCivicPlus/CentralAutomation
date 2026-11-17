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
from .importdata import ImportOptions, check_workbook, files_search, find_file, is_duplicate, load_meetings
from .models import ALL_KINDS, Agenda, Category, Item, Meeting
from ..reporting import ActionResult, Reporter
from .site import AgendaCenterSite

AGENDA_ACTIONS = ("scan_agendas", "unpublish_agendas", "delete_agendas", "publish_agendas")
ITEM_ACTIONS = ("scan_items", "approve_items", "delete_items")
CATEGORY_ACTIONS = ("publish_categories", "unpublish_categories", "delete_categories")
ALL_ACTIONS = AGENDA_ACTIONS + ITEM_ACTIONS + CATEGORY_ACTIONS + ("create_categories", "upload_agendas")


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
            return f"create_categories: {', '.join(p.get('names') or [])}" + (" (published)" if p.get("publish") else " (draft)")
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
            for it in (x for x in group if x.default):
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
        if contents["agendas"]:
            self._delete_agendas(label, {"filter": {"categories": [cat.name]}})
        items = self.site.items(cat) + self.site.items(cat, default=True)
        if items:
            self._item_actions(label, "delete", items)
        for f in self.site.folders(cat):
            self.cancel.check()
            try:
                st = self.site.delete_folder(cat, f)
            except Exception as e:
                st = f"error: {e}"
            self._res(label, "delete", "item folder", category=cat.name, target_id=str(f.id), description=f.name,
                      status="ok" if st == "ok" else "error", detail="" if st == "ok" else st)
        for w in self.site.workflows(cat):
            self.cancel.check()
            try:
                st = self.site.delete_workflow(cat, w)
            except Exception as e:
                st = f"error: {e}"
            self._res(label, "delete", "workflow", category=cat.name, target_id=str(w.id), description=w.name,
                      status="ok" if st == "ok" else "error", detail="" if st == "ok" else st)
        return self._contents_summary(self.site.contents(cat))

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
            if st == "ok" and action == "unpublish":
                self.r.record_undo({"undo": "republish_category", "category_id": cat.id, "category": cat.name})
            self._res(label, action, "category", category=cat.name, target_id=str(cat.id),
                      status="ok" if st == "ok" else "error", detail="" if st == "ok" else st)

    def _create_categories(self, label: str, params: dict) -> None:
        publish = bool(params.get("publish"))
        for name in params.get("names") or []:
            self.cancel.check()
            existing = self.site.find_category(name, refresh=True)
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
        search_dirs = [p for p in [files_dir] if p]
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
            cat = self.site.find_category(cat_name, refresh=True)
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
            created: list[int] = []
            needs_minutes: list[tuple[int, Meeting]] = []
            failures = 0
            for m in group:
                self.cancel.check()
                done_total += 1
                self.r.progress(done_total, total, f"{cat_name} {m.meeting_date}")
                if m.problems:
                    self._meeting_res(label, m, "skipped", "; ".join(m.problems))
                    continue
                if cat is not None and on_duplicate == "skip":
                    dup = next((a for a in existing if a.meeting_date == m.meeting_date
                                and is_duplicate(m, a, self.site.details)), None)
                    if dup is not None:
                        self._meeting_res(label, m, "skipped", f"already on the site as agenda {dup.id}")
                        continue
                if not self.live:
                    self._meeting_res(label, m, "dry-run", f"would create {m.kind} agenda"
                                      + (" and publish" if publish else " as draft"))
                    continue
                try:
                    new_id, st = self.site.create_agenda(cat, m, resolve)
                except Exception as e:
                    new_id, st = None, f"error: {e}"
                if new_id is not None:
                    created.append(new_id)
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

            if not self.live or not created:
                continue
            # Minutes can only be added to a published agenda, so agendas that get
            # minutes later are published even on a draft upload, then unpublished.
            to_publish = set(created) if publish else {i for i, _ in needs_minutes}
            if to_publish:
                self.r.info(f"{cat_name}: publishing {len(to_publish)} new agenda(s) (Notify Me: Don't Send)")
                fresh = [a for a in self.site.agendas(cat) if a.id in to_publish and not a.published]
                published_ok = self._bulk(label, "publish", fresh)
            else:
                published_ok = set()
            for agenda_id, m in needs_minutes:
                self.cancel.check()
                if agenda_id not in published_ok:
                    self._meeting_res(label, m, "error", "minutes not added: the agenda could not be published",
                                      agenda_id=agenda_id)
                    continue
                try:
                    st = self.site.add_minutes(cat, agenda_id, resolve(m.later_minutes), publish=publish)
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
