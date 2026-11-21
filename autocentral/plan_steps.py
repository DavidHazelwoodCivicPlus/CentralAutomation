"""The Plan: a list of changes made in the window (move these pages, fix these
typos, move these staff, delete these FAQs ...), saved, and then run in
order: a dry run first, then for real, one results list and one undo file.

Step kinds:
  module_action  publish / unpublish / delete ticked rows of a module (actions/jobs.py)
  move_pages     pages to new parent pages (Live Edit's own move)
  move_staff     departments under new parents, employees to new departments
  replace        a find and replace in pages' text blocks (editing.PageEditor)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .actions.jobs import ModuleJobs, Selection
from .editing import PageEditor, Rule, apply_rule, plain
from .reporting import ActionResult, Reporter


@dataclass
class Step:
    kind: str
    title: str
    params: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(d["kind"], d.get("title", ""), d.get("params") or {}, d.get("id") or uuid.uuid4().hex[:8])


def save_plan(path: Path, steps: list[Step]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"steps": [s.to_dict() for s in steps]}, indent=2), encoding="utf-8")


def load_plan(path: Path) -> list[Step]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Step.from_dict(s) for s in data.get("steps", [])]


# ------------------------------------------------------------ making steps
def module_step(module: str, label: str, action: str, sel: Selection) -> Step:
    return Step("module_action", f"{action.capitalize()} {sel.describe()} in {label}",
                {"module": module, "action": action, "categories": {str(k): v for k, v in sel.categories.items()},
                 "items": {str(c): {str(i): n for i, n in its.items()} for c, its in sel.items.items()}})


def move_pages_step(moves: list[dict], parent_name: str) -> Step:
    """moves: [{"id", "name", "from", "from_name", "to"}]"""
    return Step("move_pages", f"Move {len(moves)} page(s) under {parent_name}", {"moves": moves})


def move_staff_step(moves: list[dict], dept_name: str) -> Step:
    """moves: [{"kind": "department"|"employee", "id", "name", "from", "to"}]"""
    return Step("move_staff", f"Move {len(moves)} department(s) / employee(s) to {dept_name}", {"moves": moves})


def replace_step(rule: Rule, targets: dict[int, dict]) -> Step:
    """targets: {page id: {"name", "published", "blocks": [block ids], "positions": [their positions]}}"""
    n = sum(len(t["blocks"]) for t in targets.values())
    return Step("replace", f"Replace {rule.describe()} in {n} text block(s) on {len(targets)} page(s)",
                {"rule": rule.to_dict(), "targets": {str(k): v for k, v in targets.items()}})


# ------------------------------------------------------------------ running
class PlanJobs:
    """What the run window drives for a plan (same shape as ModuleJobs)."""

    def __init__(self, browser, reporter: Reporter, live: bool) -> None:
        browser.r = reporter
        self.b = browser
        self.r = reporter
        self.live = live

    def _res(self, step: str, action: str, ttype: str, name: str, status: str, detail: str = "",
             tid="", category: str = "") -> None:
        self.r.result(ActionResult(step=step, action=action, target_type=ttype, category=category,
                                   target_id=str(tid), description=name, status=status, detail=detail))

    def run(self, steps: list[Step]) -> None:
        t_all = time.monotonic()
        for n, st in enumerate(steps, 1):
            self.b.cancel.check()
            t0 = time.monotonic()
            label = f"{n}/{len(steps)}"
            self.r.info(f"=== Step {label}: {st.title}" + ("" if self.live else " (dry run)"))
            try:
                getattr(self, "_" + st.kind)(label, st)
            except Exception as e:
                if type(e).__name__ == "Cancelled":
                    raise
                self.r.error(f"Step {label} stopped: {e}")
                self._res(label, st.kind, "step", st.title, "error", str(e))
            self.r.info(f"Step {label} took {time.monotonic() - t0:.0f}s.")
        self.r.info(f"Plan finished in {time.monotonic() - t_all:.0f}s. {self.r.summary()}")

    # module publish / unpublish / delete
    def _module_action(self, label: str, st: Step) -> None:
        p = st.params
        sel = Selection(p["module"], categories={int(k): v for k, v in p.get("categories", {}).items()},
                        items={int(c): {int(i): n for i, n in its.items()} for c, its in p.get("items", {}).items()})
        jobs = ModuleJobs(self.b, self.r, self.live, p["module"])
        if p["action"] == "delete":
            jobs.delete(sel)
        else:
            jobs.set_published(sel, p["action"] == "publish")

    # pages to new parents
    def _move_pages(self, label: str, st: Step) -> None:
        from .actions.engine import Relay
        relay = Relay(self.b)
        for m in st.params["moves"]:
            what = f"under page {m['to']}" + (f" ({m.get('to_name')})" if m.get("to_name") else "")
            if not self.live:
                self._res(label, "move", "page", m["name"], "dry-run", "would move " + what, m["id"])
                continue
            r = relay.call("pages", "move", m["id"], m["to"]) or {}
            if r.get("ok"):
                self._res(label, "move", "page", m["name"], "ok", "moved " + what, m["id"])
                self.r.record_undo({"plan": "move_page", "id": m["id"], "name": m["name"], "to": m.get("from")})
            else:
                self._res(label, "move", "page", m["name"], "error", r.get("why") or "the site refused the move", m["id"])

    # departments and employees to new places
    def _move_staff(self, label: str, st: Step) -> None:
        from .actions.engine import Relay
        relay = Relay(self.b)
        for m in st.params["moves"]:
            what = f"to department {m['to']}" + (f" ({m.get('to_name')})" if m.get("to_name") else "")
            if not self.live:
                self._res(label, "move", m["kind"], m["name"], "dry-run", "would move " + what, m["id"])
                continue
            if m["kind"] == "department":
                r = relay.call("staff", "moveDept", m["id"], m["to"]) or {}
            else:
                r = relay.call("staff", "moveEmployee", m["id"], m["from"], m["to"]) or {}
            if r.get("ok"):
                self._res(label, "move", m["kind"], m["name"], "ok", "moved " + what, m["id"])
                self.r.record_undo({"plan": "move_staff", "kind": m["kind"], "id": m["id"], "name": m["name"],
                                    "from": m["to"], "to": m.get("from")})
            else:
                self._res(label, "move", m["kind"], m["name"], "error", f"the site refused the move (HTTP {r.get('status')})", m["id"])

    # find and replace in text blocks
    def _replace(self, label: str, st: Step) -> None:
        rule = Rule(**st.params["rule"])
        targets = {int(k): v for k, v in st.params["targets"].items()}
        if not self.live:
            for pid, t in targets.items():
                self._res(label, "replace", "page", t["name"], "dry-run",
                          f"would change {len(t['blocks'])} text block(s): {rule.describe()}"
                          + ("; then publish again" if t.get("published") else ""), pid)
            return
        with PageEditor(self.b, self.r.info) as ed:
            for n, (pid, t) in enumerate(targets.items(), 1):
                self.b.cancel.check()
                t0 = time.monotonic()
                wanted = set(t["blocks"])
                spots = set(t.get("positions") or [])

                def change(bid: str, html: str, pos: int) -> Optional[str]:
                    # the block read earlier, or (if the page was saved as a new version since,
                    # which gives its blocks new ids) the block in the same place
                    if bid not in wanted and pos not in spots:
                        return None
                    new, count = apply_rule(html, rule)
                    return new if count else None
                try:
                    res = ed.edit_page(pid, change, bool(t.get("published")))
                except Exception as e:
                    if type(e).__name__ == "Cancelled":
                        raise
                    res = {"blocks": {}, "error": str(e)}
                for bid, (old, new, pos) in res["blocks"].items():
                    self.r.record_undo({"plan": "set_block", "page": pid, "name": t["name"], "block": bid,
                                        "position": pos, "html": old, "new": new,
                                        "published": bool(t.get("published"))})
                changed = len(res["blocks"])
                if res["error"]:
                    self._res(label, "replace", "page", t["name"], "error", res["error"], pid)
                elif changed:
                    self._res(label, "replace", "page", t["name"], "ok",
                              f"changed {changed} text block(s)" + ("; published again" if t.get("published") else ""), pid)
                else:
                    self._res(label, "replace", "page", t["name"], "skipped", "nothing to change any more", pid)
                self.r.progress(n, len(targets), "pages")
                self.r.info(f"{t['name']}: {time.monotonic() - t0:.1f}s")

    # ---------------------------------------------------------------- undo
    def undo(self, entries: list[dict]) -> None:
        """Put back what a plan's live run changed: module changes (actions/jobs.py),
        page and staff moves, text blocks (their old HTML)."""
        from .actions.engine import Relay
        relay = Relay(self.b)
        by_module: dict[str, list[dict]] = {}
        for e in entries:
            if e.get("module"):
                by_module.setdefault(e["module"], []).append(e)
        for mod, es in by_module.items():
            ModuleJobs(self.b, self.r, self.live, mod).undo(es)
        for e in [e for e in entries if e.get("plan") in ("move_page", "move_staff")]:
            if not e.get("to"):
                self._res("undo", "move", "page", e["name"], "skipped", "the old place wasn't recorded", e["id"])
                continue
            if not self.live:
                self._res("undo", "move", e.get("kind", "page"), e["name"], "dry-run", f"would move back to {e['to']}", e["id"])
                continue
            if e["plan"] == "move_page":
                r = relay.call("pages", "move", e["id"], e["to"]) or {}
            elif e["kind"] == "department":
                r = relay.call("staff", "moveDept", e["id"], e["to"]) or {}
            else:
                r = relay.call("staff", "moveEmployee", e["id"], e["from"], e["to"]) or {}
            self._res("undo", "move", e.get("kind", "page"), e["name"], "ok" if r.get("ok") else "error",
                      "moved back" if r.get("ok") else "the site refused", e["id"])
        blocks: dict[int, list[dict]] = {}
        for e in entries:
            if e.get("plan") == "set_block":
                blocks.setdefault(int(e["page"]), []).append(e)
        if not blocks:
            return
        if not self.live:
            for pid, es in blocks.items():
                self._res("undo", "replace", "page", es[0]["name"], "dry-run", f"would put back {len(es)} text block(s)", pid)
            return
        with PageEditor(self.b, self.r.info) as ed:
            for pid, es in blocks.items():
                def back(bid: str, html: str, pos: int, es=es) -> Optional[str]:
                    # the block the plan changed: same id, or same place and still saying what the plan wrote
                    for e in es:
                        if bid == e["block"] or (pos == e.get("position") and plain(html) == plain(e.get("new", ""))):
                            return e["html"]
                    return None
                res = ed.edit_page(pid, back, any(e.get("published") for e in es))
                self._res("undo", "replace", "page", es[0]["name"], "error" if res["error"] else "ok",
                          res["error"] or f"put back {len(res['blocks'])} text block(s)", pid)
