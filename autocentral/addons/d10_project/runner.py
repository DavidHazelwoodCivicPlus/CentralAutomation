"""The project run: each step in order, each safe to run again (what's done
is skipped), everything recorded in project.json and the tracking workbook.

  plan        read the exports, decide where everything goes (no site)
  download    the files and pictures the content uses, from the old site
  folders     the project's top folders in Document Center and Public Images
  files       Content Import: the files and pictures
  index files where each one landed (old address -> new address)
  content     Content Import: pages, news, jobs, staff, archive, with file
              addresses already pointing at the new files
  index pages where each page landed; any under the wrong parent is moved
  publish     pages that were published on the old site
  relink      page-to-page links, through Live Edit (the only slow step)
  calendar    events, through the Calendar module's own import
  directory   businesses, through the Resource Directory's own import
              (never with "delete all existing entries")
  agendas     the D10 Agenda Center tool's whole run
  report      the tracking workbook

Subscribers are never notified. Nothing is deleted.
"""

from __future__ import annotations

import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from . import prompts, workbook
from .content import rewrite
from .exports import D10Site, load
from .project import Options, Project, now, plan

STEPS = [
    ("plan", "Read the D10 files and plan the site"),
    ("download", "Download the files and pictures"),
    ("folders", "Make the project folders"),
    ("files", "Import the files (Content Import)"),
    ("index_files", "Find where each file landed"),
    ("content", "Import pages, news, jobs, staff, archive"),
    ("index_pages", "Find where each page landed"),
    ("publish", "Save and publish the pages (Live Edit)"),
    ("relink", "Relink pages to pages (Live Edit)"),
    ("staff_photos", "Add the staff photos (Staff Directory)"),
    ("calendar", "Import the calendar events"),
    ("directory", "Import the Resource Directory"),
    ("agendas", "Agenda Center (the D10 Agenda Center tool)"),
    ("finish", "Publish everything that should be published"),
    ("check", "Check the site against the D10 files"),
    ("report", "Write the tracking workbook"),
]
SITE_STEPS = {k for k, _ in STEPS} - {"plan", "download", "report"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


class StepFailed(Exception):
    pass


@dataclass
class Folders:
    docs_id: int = 0
    images_id: int = 0


class ProjectRun:
    """browser: the tool's logged-in Browser (None for the offline steps)."""

    def __init__(self, exports: Path, work: Path, browser=None, log: Callable[[str], None] = print,
                 opts: Optional[Options] = None, sitemap: Optional[Path] = None,
                 on_step: Callable[[str, str], None] = lambda k, s: None) -> None:
        self.exports, self.work = Path(exports), Path(work)
        self.b = browser
        self.say = log
        self.on_step = on_step
        self.opts = opts or Options()
        self.sitemap = sitemap
        self.site: Optional[D10Site] = None
        self.pr: Optional[Project] = Project.load(self.work)
        self._relay = None

    # ---------------------------------------------------------- plumbing
    def d10(self) -> D10Site:
        if self.site is None:
            self.site = load(self.exports)
        return self.site

    @property
    def relay(self):
        if self._relay is None:
            from ...actions.engine import Relay

            class Steady(Relay):
                """A request that fails at the network level ("Failed to fetch")
                is tried once more from a fresh admin page: a page left after a
                form post can stop answering, and opening another page also
                keeps the login alive."""
                def call(inner, mod, op, *args, **kw):
                    try:
                        return Relay.call(inner, mod, op, *args, **kw)
                    except Exception as e:
                        if "Failed to fetch" not in str(e):
                            raise
                        inner.b.get("/Admin/Facilities")
                        time.sleep(2)
                        return Relay.call(inner, mod, op, *args, **kw)
            self._relay = Steady(self.b)
        return self._relay

    def cancelled(self) -> bool:
        """Also answers Central's Session Timeout box ("stay signed in") when
        it's up: this is asked between every item of the long steps."""
        if self.b is not None:
            try:
                self.b.handle_session_timeout()
            except Exception:
                pass
        return bool(self.b and self.b.cancel.cancelled)

    def run(self, steps: Optional[list[str]] = None, force: bool = False) -> Project:
        want = steps or [k for k, _ in STEPS]
        if self.pr is not None:            # choices that can change between runs (where things go is fixed by the plan)
            for k in ("publish_pages", "zone", "agenda_empty_first"):
                self.pr.options[k] = getattr(self.opts, k)
        for key, label in STEPS:
            if key not in want:
                continue
            if self.cancelled():
                break
            if key in SITE_STEPS and self.b is None:
                self.say(f"{label}: needs the Central site (log in first).")
                break
            done = self.pr and self.pr.steps.get(key, {}).get("state") == "done"
            if done and not force and key not in ("report",):
                self.say(f"=== {label}: already done")
                self.on_step(key, "done")
                continue
            self.say(f"=== {label}")
            self.on_step(key, "working")
            t = time.monotonic()
            try:
                note = getattr(self, "step_" + key)() or ""
            except Exception as e:
                if self.pr:
                    self.pr.mark(key, "failed", f"{type(e).__name__}: {e}")
                self.on_step(key, "failed")
                self.say(f"!!! {label} failed: {e}")
                if not isinstance(e, StepFailed):
                    self.say(traceback.format_exc(limit=4))
                raise
            self.pr.mark(key, "done", f"{note} ({time.monotonic() - t:.0f}s)".strip())
            self.on_step(key, "done")
            self.say(f"    {note}")
        return self.pr

    # ---------------------------------------------------------- offline
    def step_plan(self) -> str:
        from . import htmltool
        from .agenda_tool.agendacenter.d10engine import newest_tool
        tool_dir = self.work / "tool"
        if self.sitemap is None and not (tool_dir / "site-map.csv").is_file():
            tool = newest_tool(Path(__file__).parent / "agenda_tool")
            if tool:
                self.say("  running the D10-to-Central Migration Tool (site map, Resource Directory)…")
                try:
                    (tool_dir / "source").mkdir(parents=True, exist_ok=True)
                    srcs = {n: tool_dir / "source" / n for n in ("home.html", "sitemap.html")
                            if (tool_dir / "source" / n).is_file()}
                    if "home.html" not in srcs and self.b is not None:
                        srcs = htmltool.fetch_with(self.b.d, self.d10().host, tool_dir / "source")
                    htmltool.run(tool, self.exports, self.d10().host, self.opts.zone, tool_dir, self.say, srcs or None)
                except Exception as e:
                    self.say(f"  the migration tool didn't finish ({e}); planning from the exports alone")
        if self.sitemap is None and (tool_dir / "site-map.csv").is_file():
            self.sitemap = tool_dir / "site-map.csv"
        keep = self.pr
        self.pr = plan(self.d10(), self.work, self.opts, self.sitemap)
        if keep is not None:                    # a re-plan keeps what's already on the site
            self.pr.central, self.pr.steps = keep.central, {**keep.steps, "plan": self.pr.steps["plan"]}
            old = {f["mid"]: f for f in keep.files}
            for f in self.pr.files:
                o = old.get(f["mid"])
                if o:
                    for k in ("download", "size", "imported", "new_id", "new_url", "file", "folder"):
                        f[k] = o[k]
            for kind in ("news", "events", "staff", "jobs", "archive", "business"):
                done = {r["nid"]: r for r in getattr(keep, kind)}
                for r in getattr(self.pr, kind):
                    o = done.get(r["nid"])
                    if o and o["state"] not in ("planned", "failed"):
                        r["state"], r["detail"] = o["state"], o.get("detail", "")
            self.pr.links = keep.links
            oldp = {p["key"]: p for p in keep.pages}
            for p in self.pr.pages:
                o = oldp.get(p["key"])
                if o and o.get("new_id"):
                    for k in ("state", "new_id", "new_url", "title", "relinked", "html_new", "detail"):
                        if k in o:
                            p[k] = o[k]
            self.pr.save()
        return "; ".join(f"{v} {k}" for k, v in self.pr.counts().items())

    @property
    def files_dir(self) -> Path:
        return self.work / "files"

    def step_download(self) -> str:
        from .agenda_tool.agendacenter.d10engine import IN_ZIP, FileToGet
        from .agenda_tool.agendacenter.d10files import fetch_one
        from concurrent.futures import ThreadPoolExecutor, as_completed
        self.files_dir.mkdir(parents=True, exist_ok=True)
        todo = [f for f in self.pr.files if f["download"] not in ("ok", "skipped")]
        bad = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(fetch_one, FileToGet(f["url"], f["file"], IN_ZIP), self.files_dir): f for f in todo}
            for n, fut in enumerate(as_completed(futs), 1):
                f = futs[fut]
                o = fut.result()
                f["download"] = o.result
                f["detail"] = o.reason
                p = workbook.long_path(self.files_dir / f["file"])
                f["size"] = p.stat().st_size if p.is_file() else 0
                if o.result not in ("ok", "skipped"):
                    bad += 1
                    self.say(f"  {f['file']}: {o.result}: {o.reason}")
                if n % 50 == 0:
                    self.say(f"  {n}/{len(todo)} downloaded")
                    self.pr.save()
                if self.cancelled():
                    for x in futs:
                        x.cancel()
                    self.pr.save()
                    raise StepFailed("stopped; the files already downloaded are kept")
        self.pr.save()
        mb = sum(f["size"] for f in self.pr.files) / 2**20
        return f"{len(self.pr.files) - bad} of {len(self.pr.files)} files ({mb:.0f} MB)" + (f"; {bad} couldn't be downloaded" if bad else "")

    # ---------------------------------------------------------- the site
    def _folder_ids(self) -> dict[str, list[dict]]:
        roots = {r["name"]: r for r in self.relay.call("docs", "roots")}
        out = {}
        for name, src in (("Document Center", 0), ("Public Images", 4)):
            r = roots.get(name)
            out[name] = self.relay.call("docs", "folders", r["id"], src, timeout=1800) if r else []
        return out

    def _add_folder(self, parent_id: int, name: str) -> None:
        d = self.b.d
        self.b.get(f"/Admin/DocumentCenter/Folder/Add/{parent_id}")
        box = self.b.wait_css("#txtTitle", 30)
        box.clear()
        box.send_keys(name)
        d.execute_script("document.getElementById('SavePublishFolder').click()")
        self.b.wait_until(lambda: "/Folder/Add/" not in d.current_url, 60)

    def step_folders(self) -> str:
        self.pr.central = self.b.host
        o = self.pr.options
        have = self._folder_ids()
        names = [f["name"].lower() for fs in have.values() for f in fs]
        made = []
        for root_name, top_id_root, key in (("Document Center", 1, "docs_root"), ("Public Images", None, "images_root")):
            want = o[key]
            mine = [f for f in have[root_name] if f["name"] == want and f["parent"] in (1, self._root_id(root_name))]
            if mine:
                continue
            if names.count(want.lower()):
                raise StepFailed(f"A folder named '{want}' is already somewhere else on the site; Content Import finds "
                                 "parent folders by name, so pick another name for the project folder.")
            self._add_folder(top_id_root or self._root_id(root_name), want)
            made.append(want)
        # Sub-folder names must not clash with folders elsewhere (staff photos and
        # news pictures name their folder alone).
        clash = set(names)
        renamed = 0
        for f in self.pr.files:
            if f["root"] in (o["docs_root"], o["images_root"]) and f["folder"].lower() in clash and not f.get("folder_fixed"):
                f["folder"] = f"{f['folder']} ({self.pr.site})"[:60]
                f["folder_fixed"] = True
                renamed += 1
        self.pr.save()
        return (f"made {', '.join(made)}" if made else "project folders already there") + \
               (f"; {renamed} file(s) put in renamed sub-folders (the name was taken)" if renamed else "")

    def _root_id(self, name: str) -> int:
        return next(r["id"] for r in self.relay.call("docs", "roots") if r["name"] == name)

    def _upload(self, zips: list[Path], what: str) -> list:
        from .agenda_tool.agendacenter.contentimport import UploadLog, upload_zip
        log = UploadLog(zips[0].parent) if zips else None
        reports = []
        for z in zips:
            prev = log.find(self.b.host, z)
            if prev and prev.get("state"):
                self.say(f"  {z.name}: already sent to {self.b.host} ({prev['state']}); not sent again")
                continue
            log.record(self.b.host, z, "uploading")
            self.say(f"  sending {z.name} ({z.stat().st_size / 2**20:.0f} MB)…")
            rep = upload_zip(self.b, z, max_wait=3 * 3600)
            log.record(self.b.host, z, "done", f"{rep.rows} rows in {rep.seconds:.0f}s")
            fails = [ln for ln in rep.text.splitlines() if "failed" in ln.lower() and "import failed" in ln.lower()
                     and "Object reference" not in ln]
            self.say(f"  {z.name}: {rep.rows} row(s) in {rep.seconds:.0f}s" + (f"; {len(fails)} failed" if fails else ""))
            for ln in fails[:20]:
                self.say("    " + ln.strip()[:300])
            (self.work / "import-reports").mkdir(exist_ok=True)
            (self.work / "import-reports" / (z.stem + ".txt")).write_text(rep.text, encoding="utf-8")
            reports.append(rep)
        return reports

    def step_files(self) -> str:
        zips = workbook.write_files_batches(self.pr.files, self.files_dir, self.work / "batches", self.pr.site, log=self.say)
        if not zips:
            return "no files to import"
        self._upload(zips, "files")
        return f"{len(zips)} zip(s) sent"

    def step_index_files(self) -> str:
        o = self.pr.options
        have = self._folder_ids()
        found = 0
        for root_name, top in (("Document Center", o["docs_root"]), ("Public Images", o["images_root"])):
            src = 0 if root_name == "Document Center" else 4
            folders = have[root_name]
            top_ids = [f["id"] for f in folders if f["name"] == top]
            if not top_ids:
                continue
            mine = [f for f in folders if f["id"] in top_ids or f["parent"] in top_ids]
            names = {f["id"]: f["name"] for f in mine}
            listed = self.relay.call("docs", "files", [f["id"] for f in mine], src, timeout=3600)
            # Central stores a file as "<Display Name>_<hash>.ext", so a file is
            # known by its folder and display name; same names go in upload
            # order, which is the order of the site's IDs.
            by: dict[tuple, list] = {}
            for x in sorted(listed, key=lambda x: x["id"]):
                by.setdefault((names.get(x["folder"], "").lower(), norm(x["name"])), []).append(x)
            for f in self.pr.files:
                if f["root"] != top or f.get("new_id"):
                    continue
                hits = by.get((f["folder"].lower(), norm(f["title"])))
                if hits:
                    x = hits.pop(0)
                    f["imported"], f["new_id"] = "yes", x["id"]
                    link = x.get("link") or ""
                    path = urlparse(link).path if link.startswith("http") else link
                    f["new_url"] = path or (f"/ImageRepository/Document?documentID={x['id']}" if f["image"]
                                            else f"/DocumentCenter/View/{x['id']}")
                    found += 1
        self.pr.save()
        missing = [f for f in self.pr.files if f["root"] != "Archive Center" and f["download"] in ("ok", "skipped")
                   and f["imported"] != "yes"]
        for f in missing[:15]:
            self.say(f"  not found on the site: {f['folder']}/{f['file']}")
        return f"{found} file(s) found" + (f"; {len(missing)} not found" if missing else "")

    # ---------------------------------------------------------- addresses
    def resolver(self, pages_too: bool) -> Callable[[str], Optional[str]]:
        site = self.d10()
        files = {f["mid"]: f for f in self.pr.files}
        pages = {p["nid"]: p for p in self.pr.pages if p["nid"]}

        def resolve(url: str) -> Optional[str]:
            if not url or url.startswith(("mailto:", "tel:", "#", "javascript:")):
                return None
            m = site.media_for_url(url)
            if m:
                f = files.get(m.id)
                return f["new_url"] if f and f.get("new_url") else None
            if pages_too:
                n = site.node_for_url(url)
                if n:
                    p = pages.get(n.id)
                    return p["new_url"] if p and p.get("new_url") else None
            p = urlparse(url)
            if p.netloc and p.netloc.lower() == site.host and p.path.startswith("/sites/"):
                return None             # an old file the project doesn't know: left for the report
            return None
        return resolve

    def _rewrite_all(self, pages_too: bool) -> int:
        res = self.resolver(pages_too)
        n = 0
        for kind in ("pages", "news", "jobs", "events"):
            for r in getattr(self.pr, kind):
                new, changed = rewrite(r.get("html_new", r["html"]), res)
                if changed:
                    r["html_new"] = new
                    n += len(changed)
                    where = r.get("title")
                    for old, nu in changed:
                        self.pr.links.append({"where": f"{kind}: {where}", "old": old, "new": nu,
                                              "kind": "page" if "/node/" in old or not old.startswith(("/media", "http")) else "file",
                                              "state": "in import" if not pages_too else "to edit"})
        self.pr.save()
        return n

    def _ensure_categories(self) -> list[str]:
        made = []
        want = {"archive": {a["category"] for a in self.pr.archive}, "news": {n["category"] for n in self.pr.news}}
        for mod, names in want.items():
            if not names:
                continue
            have = {c["name"].strip().lower() for c in self.relay.cats(mod)}
            for name in sorted(names):
                if name.lower() not in have:
                    self.relay.call(mod, "create", name, True)
                    made.append(f"{mod}: {name}")
        return made

    def step_content(self) -> str:
        n = self._rewrite_all(pages_too=False)
        made = self._ensure_categories()
        self.pr.before_pages = [p["id"] for p in self.relay.call("pages", "list")]
        self.pr.save()
        zips = workbook.write_content_zip(self.pr, self.work / "batches", self.files_dir, log=self.say)
        self._upload(zips, "content")
        for kind in ("news", "jobs", "staff", "archive"):
            for r in getattr(self.pr, kind):
                if r["state"] == "planned":
                    r["state"] = "imported"
        self.pr.save()
        return f"{n} file address(es) pointed at the new files" + (f"; made {', '.join(made)}" if made else "")

    def step_index_pages(self) -> str:
        before = set(self.pr.before_pages or [])
        now_ids = [p["id"] for p in self.relay.call("pages", "list")]
        new_ids = [i for i in now_ids if i not in before] if before else now_ids
        read = []
        for i in range(0, len(new_ids), 60):
            read += self.relay.call("pages", "read", new_ids[i:i + 60], True, timeout=1800)
        by_title: dict[str, list[dict]] = {}
        for r in read:
            if r.get("title"):
                by_title.setdefault(r["title"].strip().lower(), []).append(r)
        mine = {p["key"]: p for p in self.pr.pages}
        root_id = 1
        found = moved = 0
        for p in self.pr.pages:
            if p.get("new_id"):
                found += 1
                continue
            hits = by_title.get(p["title"].lower(), [])
            parent = mine.get(p["parent"])
            want_parent = parent["new_id"] if parent else root_id
            hit = next((h for h in hits if h.get("parent") == want_parent), None) or (hits[0] if hits else None)
            if not hit:
                p["state"], p["detail"] = "not found", "not on the site after the import"
                continue
            hits.remove(hit)
            p["new_id"], p["state"] = hit["id"], "imported"
            slug = re.sub(r"[^A-Za-z0-9]+", "-", p["title"]).strip("-")
            p["new_url"] = f"/{hit['id']}/{slug}"
            found += 1
            if want_parent and hit.get("parent") != want_parent:
                r = self.relay.call("pages", "move", hit["id"], want_parent)
                if r.get("ok"):
                    moved += 1
                else:
                    p["detail"] = f"under the wrong page; moving it failed: {r.get('why')}"
        self.pr.save()
        missing = [p["title"] for p in self.pr.pages if p["state"] == "not found"]
        return f"{found} of {len(self.pr.pages)} page(s) found" + (f", {moved} moved under the right page" if moved else "") + \
               (f"; not found: {', '.join(missing[:10])}" if missing else "")

    def step_publish(self) -> str:
        """Content Import leaves each page locked in Live Edit with its changes
        unsaved (a working copy), then reports "Object reference not set..."
        (seen on training19, 2026-11-20). Saving it in Live Edit finishes it:
        Save and Publish for pages published on the old site, Save for the rest.
        About 4 s a page, parents first."""
        d = self.b.d
        todo = [p for p in self.pr.pages if p.get("new_id") and p["state"] in ("imported", "saved", "not saved")]
        publish_ok = self.pr.options.get("publish_pages", True)
        done = failed = 0
        home = d.current_window_handle
        d.switch_to.new_window("tab")
        try:
            self.b.get("/Admin")
            d.execute_script("document.cookie = 'enableLiveEdit=true; path=/';")
            for n, p in enumerate(todo, 1):
                if self.cancelled():
                    break
                pub = p["published"] and publish_ok
                try:
                    d.get(self.b.url(f"/{p['new_id']}"))
                    self.b.wait_until(lambda: d.execute_script("return document.readyState") == "complete", 60)
                    locked = d.execute_script("return typeof SaveVersion === 'function' && /locked this version/i.test(document.body.innerText)")
                    if locked:
                        d.execute_script("SaveVersion(arguments[0]);", "publish" if pub else "")
                        self.b.wait_until(lambda: not d.execute_script(
                            "return /locked this version/i.test(document.body.innerText)"), 90)
                    elif pub:
                        r = self.relay.call("pages", "catAct", "publish", [p["new_id"]])
                        if r.get("failed"):
                            raise RuntimeError(str(list(r["failed"].values())[0]))
                    wc = d.execute_script("const v = document.getElementById('hdnVersionID'); if (!v) return 'no version';"
                                          "const x = new XMLHttpRequest(); x.open('GET', '/Pages/Version/HasWorkingCopy?versionID=' + v.value, false);"
                                          "x.send(); return x.responseText;")
                    if "true" in str(wc).lower():
                        raise RuntimeError("still has unsaved Live Edit changes")
                    p["state"] = "published" if pub else "saved"
                    p["detail"] = ""
                    done += 1
                except Exception as e:
                    p["state"], p["detail"] = "not saved", f"saving in Live Edit: {e}"[:300]
                    failed += 1
                    self.say(f"  {p['title']}: {p['detail']}")
                if n % 10 == 0:
                    self.say(f"  {n}/{len(todo)} pages saved")
                    self.pr.save()
        finally:
            try:
                d.execute_script("document.cookie = 'enableLiveEdit=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';")
                d.close()
            finally:
                d.switch_to.window(home)
        self.pr.save()
        pubs = sum(1 for p in self.pr.pages if p["state"] == "published")
        return f"{done} page(s) saved ({pubs} published)" + (f"; {failed} couldn't be saved" if failed else "")

    def step_relink(self) -> str:
        from ...editing import PageEditor
        res = self.resolver(pages_too=True)
        todo = [p for p in self.pr.pages if p.get("new_id") and p["state"] in ("saved", "published")
                and not p.get("relinked") and (p["nodes"] or rewrite(p.get("html_new", p["html"]), res)[1])]
        edited = 0
        if not todo:
            return "no page-to-page links to fix"
        with PageEditor(self.b, self.say) as ed:
            for p in todo:
                if self.cancelled():
                    break

                def change(bid, html, pos):
                    new, changed = rewrite(html, res)
                    return new if changed else None
                try:
                    out = ed.edit_page(p["new_id"], change, publish=p["state"] == "published")
                except Exception as e:                 # one page's trouble doesn't stop the rest
                    if self.cancelled():
                        raise
                    out = {"blocks": {}, "error": f"{type(e).__name__}: {e}"[:300], "retry": True}
                if out["error"]:
                    p["detail"] = "relink: " + out["error"]
                    if "working copy" in out["error"]:
                        p["state"], out["retry"] = "imported", True        # the publish step saves it; relink again after
                    self.say(f"  {p['title']}: {out['error']}")
                elif out["blocks"]:
                    edited += 1
                    for ln in self.pr.links:
                        if ln["where"] == f"pages: {p['title']}" and ln["state"] == "to edit":
                            ln["state"] = "done"
                p["relinked"] = not out.get("retry")
                self.pr.save()
        # anything still pointing at the old site
        for p in self.pr.pages:
            for old in re.findall(r'(?:href|src)="(https?://' + re.escape(self.pr.old_host) + r'[^"]*)"', p.get("html_new", p["html"])):
                self.pr.links.append({"where": f"pages: {p['title']}", "old": old, "new": "", "kind": "old site",
                                      "state": "check"})
        self.pr.save()
        return f"{edited} of {len(todo)} page(s) relinked"

    # ---------------------------------------------------------- staff photos
    def _staff_depts(self) -> dict[str, int]:
        """Department name -> id (the newest one when a name is used twice)."""
        out: dict[str, int] = {}
        for c in self.relay.cats("staff"):
            k = c["name"].strip().lower()
            out[k] = max(out.get(k, 0), c["id"])
        return out

    def _open_employee(self, eid: int, dept: int) -> None:
        self.b.get("/Admin/Directory.aspx")
        self.b.d.execute_script(
            "const f = document.forms.frmCityDepartmentDetail;"
            "const set = (n, v) => { let e = f.elements[n]; if (!e) { e = document.createElement('input');"
            " e.type = 'hidden'; e.name = n; f.append(e); } e.value = v; };"
            "set('strPage', 'ItemForm'); set('lngCityEmployeeID', arguments[0]); set('lngCityDepartmentID', arguments[1]);"
            "set('ysnSave', 0); f.submit();", eid, dept)
        self.b.wait_css("form[name=frmCityEmployeeInfo]", 30)
        time.sleep(1.0)

    def step_staff_photos(self) -> str:
        """Content Import leaves out anyone whose row names a photo, so people
        go in without one and the photo is set here, in each person's own form:
        the picture already in the project's Public Images folder, then Save and
        Publish (never notifying anyone). About 6 s a person."""
        d = self.b.d
        depts = self._staff_depts()
        todo = [s for s in self.pr.staff if s["photo"] and not s.get("photo_set")]
        done = 0
        cache: dict[int, list] = {}
        for s in todo:
            if self.cancelled():
                break
            f = self.pr.file(s["photo"])
            if not f or not f.get("new_id"):
                s["detail"] = "photo not in Central (not downloaded or not imported)"
                continue
            dept = depts.get(s["department"].strip().lower())
            if not dept:
                s["detail"] = f"department '{s['department']}' not found"
                continue
            if dept not in cache:
                cache[dept] = self.relay.call("staff", "employees", [dept])
            want = " ".join(f"{s['last']}, {s['first']}".lower().split())
            emp = next((e for e in cache[dept] if " ".join(e["name"].lower().split()) == want), None)
            if not emp:
                s["detail"] = "person not found in the department"
                continue
            try:
                self._open_employee(emp["id"], dept)
                d.execute_script(
                    "const f = document.forms.frmCityEmployeeInfo; f.elements.imgSrc.value = arguments[0];"
                    "document.getElementById('employeeImageLocation').value = '/ImageRepository/Document?documentID=' + arguments[0];"
                    "if (f.elements.imgAlt) f.elements.imgAlt.value = arguments[1];"
                    "if (f.elements.ImageDocumentId) f.elements.ImageDocumentId.value = arguments[0];",
                    f["new_id"], f["alt"] or f"{s['first']} {s['last']}")
                btn = d.execute_script("return [...document.forms.frmCityEmployeeInfo.querySelectorAll('input[type=submit],button')]"
                                       ".find(b => (b.value || b.textContent).trim() === 'Save and Publish')")
                d.execute_script("arguments[0].click()", btn)
                # People with no phone or email get "...wasn't provided... Proceed?": OK.
                prompts.answer(d, lambda: not d.find_elements("css selector", "form[name=frmCityEmployeeInfo]"),
                               timeout=60, quiet=0.5, log=self.say)
                self.b.answer_notify_dont_send(appear_timeout=1) if self.b.notify_modal_open() else None
                self.b.wait_until(lambda: not d.find_elements("css selector", "form[name=frmCityEmployeeInfo]"), 60)
                s["photo_set"], s["state"], s["detail"] = True, "published", ""
                done += 1
            except Exception as e:
                s["detail"] = f"setting the photo: {e}"[:300]
                self.say(f"  {s['first']} {s['last']}: {s['detail']}")
            self.pr.save()
        self.pr.save()
        return f"{done} of {len(todo)} photo(s) set"

    # ---------------------------------------------------------- finish
    def step_finish(self) -> str:
        """Everything the project made that was published on the old site ends
        up published: staff departments and people, news and archive
        categories and items, calendars and events, Resource Directory entries,
        and every page (read back; saved again in Live Edit when it isn't)."""
        said = []
        depts = self._staff_depts()
        mine = sorted({depts[s["department"].strip().lower()] for s in self.pr.staff
                       if s["department"].strip().lower() in depts})
        if mine:
            parents = {str(c["id"]): c.get("parent", 0) for c in self.relay.cats("staff")}
            res = self.relay.call("staff", "catAct", "publish", mine, parents)
            emps = self.relay.call("staff", "employees", mine)
            n = 0
            for dpt in mine:
                ids = [e["id"] for e in emps if e["dept"] == dpt and not e["published"]]
                if ids:
                    r = self.relay.call("staff", "itemAct", "publish", dpt, ids)
                    n += len(r.get("ok") or [])
            said.append(f"staff: {len(res.get('ok') or [])} department(s), {n} more people published")
        for mod, names in (("news", {n["category"] for n in self.pr.news}),
                           ("archive", {a["category"] for a in self.pr.archive}),
                           ("calendar", {e["calendar"] for e in self.pr.events})):
            if not names:
                continue
            cats = [c for c in self.relay.cats(mod) if c["name"].strip() in names]
            unpub = [c["id"] for c in cats if c.get("published") is False]
            if unpub:
                self.relay.act(mod, "category", "publish", unpub)
            done_ids: set = set()
            for c in cats:
                for _ in range(20):            # some lists show 50 at a time: go again until none are left
                    ids = [i["id"] for i in self.relay.items(mod, c["id"])
                           if i.get("published") is False and i["id"] not in done_ids]
                    if not ids:
                        break
                    ok, failed = self.relay.act(mod, "item", "publish", ids, c["id"])
                    done_ids.update(ok)
                    if not ok:
                        break
            n = len(done_ids)
            said.append(f"{mod}: {len(unpub)} categories, {n} item(s) published")
        names = {b["name"].strip().lower() for b in self.pr.business}
        if names and self.pr.steps.get("directory", {}).get("state") == "done":
            ids = [i["id"] for i in self.relay.items("resdir", 0)
                   if i["name"].strip().lower() in names and i.get("published") is False]
            if ids:
                ok, failed = self.relay.act("resdir", "item", "publish", ids, 0)
                said.append(f"Resource Directory: {len(ok)} published")
        ids = [p["new_id"] for p in self.pr.pages if p.get("new_id")]
        got = []
        for i in range(0, len(ids), 60):
            got += self.relay.call("pages", "read", ids[i:i + 60], True, timeout=1800)
        st = {g["id"]: g.get("status") for g in got}
        again = 0
        for p in self.pr.pages:
            if (p.get("new_id") and p["published"] and self.pr.options.get("publish_pages", True)
                    and st.get(p["new_id"]) != "published"):
                p["state"] = "imported"
                again += 1
        if again:
            self.say(f"  {again} page(s) not published yet: saving them in Live Edit")
            self.step_publish()
        pub = sum(1 for p in self.pr.pages if p.get("new_id") and p["state"] == "published")
        said.append(f"pages: {pub} of {len(ids)} published")
        if self.pr.jobs and not any(t["where"] == "Jobs" for t in self.pr.tasks):
            self.pr.tasks.append({"what": "Check", "where": "Jobs",
                                  "detail": "The tool can't publish Jobs yet: check they show on the site", "state": "to do"})
        self.pr.save()
        return "; ".join(said)

    def step_calendar(self) -> str:
        if not self.pr.events:
            return "no events"
        names = sorted({e["calendar"] for e in self.pr.events})
        have = {c["name"].strip().lower() for c in self.relay.cats("calendar")}
        for n in names:
            if n.lower() not in have:
                self.relay.call("calendar", "create", n, True)
        path = workbook.calendar_csv(self.pr.events, self.work / f"{self.pr.site}-calendar-import.csv")
        d = self.b.d
        self.b.get("/admin/calendar.aspx")
        # The page opens on the calendar last looked at; Import is on "All Calendars".
        if not d.find_elements("css selector", "#importCalendarsButton"):
            link = d.execute_script("return [...document.querySelectorAll('a')].find(a => a.textContent.trim() === 'All Calendars')")
            if link is not None:
                d.execute_script("arguments[0].click()", link)
        self.b.wait_css("#importCalendarsButton", 30)
        d.execute_script("document.getElementById('importCalendarsButton').click()")
        box = self.b.wait_css("#importCalendarsFile", 30)
        box.send_keys(str(path.resolve()))
        d.execute_script("const y = document.querySelector('#ImportCalendarsForm input[name=autoPublish][value=true]'); if (y) y.click();"
                         "document.getElementById('lnkImport').click();")
        # The result comes as a prompt ("Import results / 36 rows imported
        # successfully."), which holds the page until it's answered.
        said: list[str] = []
        end = time.time() + 900
        while time.time() < end and not said:
            said = prompts.answer(d, lambda: False, timeout=5, log=self.say)
        msg = " / ".join(said)
        if not msg:
            raise StepFailed("the Calendar import gave no result in 15 minutes; check the calendars before running it again")
        if not re.search(r"\d+\s+rows?\s+imported", msg, re.I):
            raise StepFailed(f"the Calendar import said: {msg[:300]}")
        (self.work / "import-reports").mkdir(exist_ok=True)
        (self.work / "import-reports" / "calendar.txt").write_text(msg or "", encoding="utf-8")
        for e in self.pr.events:
            e["state"] = "imported"
        for t in self.pr.tasks:
            if t["what"] == "Calendar event":
                t["state"] = "imported: check"
        self.pr.save()
        return f"{len(self.pr.events)} event(s) sent to {len(names)} calendar(s): {re.sub(r'\s+', ' ', msg or '')[:200]}"

    def step_directory(self) -> str:
        src = self.work / "tool" / "resource-directory.txt"
        if not src.is_file():
            return "no resource-directory.txt from the migration tool (run its step with the old site's pages)"
        text = src.read_text(encoding="utf-8-sig")
        res = self.resolver(pages_too=False)
        text = re.sub(r'(src|href)="([^"]+)"', lambda m: f'{m.group(1)}="{res(m.group(2)) or m.group(2)}"', text)
        out = self.work / f"{self.pr.site}-resource-directory.txt"
        out.write_text(text, encoding="utf-8")
        d = self.b.d
        before = len(self.relay.items("resdir", 0))
        self.b.get("/Admin/BusinessDirectoryii.aspx")
        main = d.current_window_handle
        handles = set(d.window_handles)
        # The module's own button: it opens the upload popup, which sends the
        # file and then hands it back to this page (opener.callImport), which
        # runs the import. A person's clicks, in that order.
        btn = self.b.wait_css("input[value='Import Items']", 30)
        d.execute_script("arguments[0].scrollIntoView({block: 'center'})", btn)
        btn.click()                     # a real click: Chrome blocks popups opened from scripts
        self.b.wait_until(lambda: len(set(d.window_handles) - handles) > 0, 30)
        popup = next(iter(set(d.window_handles) - handles))
        d.switch_to.window(popup)
        try:
            self.b.wait_css("input[name=dbase]", 30).send_keys(str(out.resolve()))
            # NEVER delete the existing entries (the popup's default is "yes").
            # The migration tool's file has no header row: "First line contains field names" = no.
            first_line = text.splitlines()[0].lower() if text else ""
            header = first_line.startswith(("name\t", "business name\t", "resource name\t"))
            d.execute_script("document.getElementById('opt2_no').checked = true; document.getElementById('opt2_yes').checked = false;"
                             "document.getElementById(arguments[0] ? 'opt1_yes' : 'opt1_no').checked = true;"
                             "document.getElementById('opt3_yes').checked = true;", header)
            if d.execute_script("return document.getElementById('opt2_yes').checked || !document.getElementById('opt2_no').checked"):
                raise StepFailed("couldn't turn off 'Delete all existing entries'; nothing was imported")
            d.execute_script("setTimeout(() => verifySubmit(), 100)")
            self.b.wait_until(lambda: popup not in d.window_handles, 300)     # it closes itself when the file is in
        finally:
            if popup in d.window_handles:
                d.switch_to.window(popup)
                d.close()
            d.switch_to.window(main)
        said = prompts.answer(d, lambda: d.execute_script("return document.readyState") == "complete",
                              timeout=900, quiet=4, log=self.say)
        after = len(self.relay.items("resdir", 0))
        msg = " / ".join(said) or "(no message)"
        (self.work / "import-reports").mkdir(exist_ok=True)
        (self.work / "import-reports" / "resource-directory.txt").write_text(
            f"{msg}\nentries before: {before}, after: {after}\n", encoding="utf-8")
        added = after - before
        if added <= 0:
            raise StepFailed(f"the Resource Directory has {after} entries, as before: nothing was imported ({msg[:200]})")
        for b in self.pr.business:
            b["state"] = "imported"
        self.pr.save()
        return f"{added} entr(ies) added from {len(text.splitlines())} line(s)" + (f"; the site said: {msg[:150]}" if said else "")

    def step_agendas(self) -> str:
        """The D10 Agenda Center tool's own run (its copy in agenda_tool)."""
        from .agenda_tool.agendacenter import d10migrate as dm
        from .agenda_tool.agendacenter.d10engine import newest_tool
        from .agenda_tool.agendacenter.jobs import AgendaJobs
        from .agenda_tool.agendacenter.parallel import DEFAULT_TABS
        from .agenda_tool.browser import Browser as AgendaBrowser
        from .agenda_tool.reporting import Reporter as AgendaReporter
        if not list(self.exports.glob("*meetings.json")):
            return "no meetings export"
        # The finishing work needs the Agenda Center admin pages; on training19
        # (2026-11-21) they showed "An error has occurred" while the public page
        # worked, and the run imported but couldn't finish anything.
        page = self.b.d.execute_async_script(
            "const done = arguments[0]; fetch('/Admin/AgendaCenter', {credentials: 'same-origin'})"
            ".then(r => r.text()).then(t => done(/An error has occurred/.test(t) ? 'error' : 'ok')).catch(e => done('ok'))")
        if page == "error":
            raise StepFailed("the site's Agenda Center admin page shows 'An error has occurred'; nothing was done. "
                             "Run this step again once it opens normally (anything already imported isn't imported again).")
        tool = newest_tool(Path(__file__).parent / "agenda_tool")
        zone = self.pr.options.get("zone") or "Central"
        work = self.work / "agenda-center"
        def part(text: str) -> None:            # which part of the Agenda Center run, shown on the checklist
            self.on_step("agendas", "working: " + text)
            self.say(f"  -- {text}")
        st = dm.MigrationState.load(work)
        if st is None or not st.built_at:
            part("read the D10 files (migration tool)")
            st = dm.build(tool, self.exports, zone, work, self.say)
        part("download the meeting files")
        st = dm.download(st, self.say, self.cancelled)
        if not st.batched_at:
            part("make the import batches")
            st = dm.make_batches(st, dm.DEFAULT_LIMIT_MB, self.say, self.cancelled)
        parts = {"Empty the Agenda Center": "empty the Agenda Center", "Create the categories": "create the categories",
                 "Content Import": "import (Content Import)", "To-do list": "finish the agendas (saves, video links, minutes)",
                 "Publish the rest": "publish the rest", "Accuracy check": "check against the D10 files"}

        def on_log(level, msg):
            m = msg.strip()
            if m.startswith("=== "):
                hit = next((v for k, v in parts.items() if m[4:].startswith(k)), None)
                if hit:
                    part(hit)
                    return
            self.say(msg)
        rep = AgendaReporter(on_log=on_log)
        rep.run_dir = work / "runs" / time.strftime("%Y%m%d-%H%M%S")
        rep.run_dir.mkdir(parents=True, exist_ok=True)
        ab = AgendaBrowser(self.b.site_url, rep, self.b.cancel)
        ab.driver = self.b.d                    # the same logged-in Chrome
        empty = bool(self.pr.options.get("agenda_empty_first")) and not self.pr.steps.get("agendas_emptied")
        report = AgendaJobs(ab, rep, live=True).migrate(st, empty_first=empty, verify=True, report=True, tabs=DEFAULT_TABS)
        if empty:
            self.pr.steps["agendas_emptied"] = {"state": "done", "at": now(), "note": "Agenda Center emptied before the import"}
        probs = work / f"{st.site}-accuracy-problems.csv"
        n = max(0, len(probs.read_text(encoding="utf-8-sig").splitlines()) - 1) if probs.is_file() else 0
        if n:
            self.pr.tasks.append({"what": "Agenda Center", "where": str(report or ""), "detail": f"{n} problem(s) in the accuracy check",
                                  "state": "to do"})
        return f"report: {report}" + (f"; {n} problem(s) in the accuracy check (see the report)" if n else "; every meeting checks out")

    # ---------------------------------------------------------- check
    def step_check(self) -> str:
        """The site against the D10 files: for each module, what the D10 files
        have that the site doesn't (missing), what the site has that they
        don't (extra) and anything on the site twice (duplicate). Read only.
        Extra means: in the project's own folders, departments and categories
        (and, for pages, made since the content import) but not in the files.
        Agenda Center has its own meeting-by-meeting check in its step."""
        rows: list[dict] = []

        def add(module, item, issue, detail=""):
            rows.append({"module": module, "item": item, "issue": issue, "detail": detail})

        def compare(module, want: list[str], have: list[str], where: str = ""):
            # Names can repeat in the D10 files themselves (19 court dates called
            # "Municipal Court"): a duplicate is more copies on the site than there.
            w: dict[str, str] = {}
            wc: dict[str, int] = {}
            for x in want:
                w[norm(x)] = x
                wc[norm(x)] = wc.get(norm(x), 0) + 1
            hc: dict[str, int] = {}
            names: dict[str, str] = {}
            for x in have:
                hc[norm(x)] = hc.get(norm(x), 0) + 1
                names[norm(x)] = x
            for k, x in w.items():
                if k not in hc:
                    add(module, x, "missing", where + (f" ({wc[k]} expected)" if wc[k] > 1 else ""))
                elif hc[k] < wc[k]:
                    add(module, x, "missing", f"{where} ({hc[k]} of {wc[k]} on the site)".strip())
            for k, n in hc.items():
                if k not in w:
                    add(module, names[k], "extra", where)
                elif n > wc[k]:
                    add(module, names[k], "duplicate", f"{where} ({n} on the site, {wc[k]} in the D10 files)".strip())

        # Pages: each planned page by its Central ID and title; extra = made since the import, not planned.
        listed = {p["id"] for p in self.relay.call("pages", "list")}
        mine = {p["new_id"] for p in self.pr.pages if p.get("new_id")}
        ids = sorted(mine & listed)
        got = []
        for i in range(0, len(ids), 60):
            got += self.relay.call("pages", "read", ids[i:i + 60], True, timeout=1800)
        title = {g["id"]: g.get("title") or "" for g in got}
        for p in self.pr.pages:
            if not p.get("new_id"):
                add("Pages", p["title"], "missing", "not imported")
            elif p["new_id"] not in listed:
                add("Pages", p["title"], "missing", f"page {p['new_id']} isn't on the site any more")
            elif norm(title.get(p["new_id"], "")) != norm(p["title"]):
                add("Pages", p["title"], "different", f"the site calls it '{title.get(p['new_id'])}'")
        if self.pr.before_pages:
            new = listed - set(self.pr.before_pages) - mine
            for g in (self.relay.call("pages", "read", sorted(new)[:200], True) if new else []):
                add("Pages", g.get("title") or str(g["id"]), "extra", f"page {g['id']}, made after the import")

        # Files and pictures: the project's own folders.
        o = self.pr.options
        have_folders = self._folder_ids()
        for root_name, top, src in (("Document Center", o["docs_root"], 0), ("Public Images", o["images_root"], 4)):
            folders = have_folders.get(root_name, [])
            top_ids = [f["id"] for f in folders if f["name"] == top]
            module = "Documents" if src == 0 else "Pictures"
            want = [f for f in self.pr.files if f["root"] == top and f["download"] in ("ok", "skipped")]
            if not top_ids:
                if want:
                    add(module, top, "missing", f"the project folder isn't in {root_name}")
                continue
            sub = {f["id"]: f["name"] for f in folders if f["id"] in top_ids or f["parent"] in top_ids}
            listed_files = self.relay.call("docs", "files", list(sub), src, timeout=3600)
            for fname in sorted({f["folder"] for f in want} | {sub[x["folder"]] for x in listed_files}):
                compare(module, [f["title"] for f in want if f["folder"] == fname],
                        [x["name"] for x in listed_files if sub.get(x["folder"]) == fname], f"folder {fname}")

        # Staff, by department (the newest department of each name).
        depts = self._staff_depts()
        for dname in sorted({s["department"] for s in self.pr.staff}):
            want = [" ".join(f"{s['last']}, {s['first']}".split()) for s in self.pr.staff if s["department"] == dname]
            did = depts.get(dname.strip().lower())
            if not did:
                for w in want:
                    add("Staff", w, "missing", f"department {dname} isn't on the site")
                continue
            have = [" ".join(e["name"].split()) for e in self.relay.call("staff", "employees", [did])]
            compare("Staff", want, have, f"department {dname}")

        # News, archive, calendar: the project's categories and their items.
        for mod, label, recs, cat_key in (("news", "News Flash", self.pr.news, "category"),
                                          ("archive", "Archive Center", self.pr.archive, "category"),
                                          ("calendar", "Calendar", self.pr.events, "calendar")):
            names = sorted({r[cat_key] for r in recs})
            if not names:
                continue
            cats = {}
            for c in self.relay.cats(mod):
                cats.setdefault(c["name"].strip(), []).append(c["id"])
            for n in names:
                want = [r["title"] for r in recs if r[cat_key] == n]
                if n not in cats:
                    for w in want:
                        add(label, w, "missing", f"{n} isn't on the site")
                    continue
                have = []
                for cid in cats[n]:
                    have += [i["name"] for i in self._all_items(mod, cid)]
                    if mod == "news":                # old news is in News Flash's archive, not its list
                        arch = norm(self._news_archive(cid))
                        have += [w for w in want if norm(w) and norm(w) in arch and norm(w) not in map(norm, have)]
                compare(label, want, have, n)

        # Resource Directory: one flat list, so "extra" is anything not in the D10 files.
        if self.pr.business:
            have = [i["name"] for i in self.relay.items("resdir", 0)]
            compare("Resource Directory", [b["name"] for b in self.pr.business], have)

        self.pr.check = rows
        self.pr.save()
        from collections import Counter
        c = Counter((r["module"], r["issue"]) for r in rows)
        for (m, i), n in sorted(c.items()):
            self.say(f"  {m}: {n} {i}")
        if not rows:
            return "everything matches the D10 files"
        return "; ".join(f"{n} {i} in {m}" for (m, i), n in sorted(c.items())) + " (see the Check sheet)"

    def _news_archive(self, cid: int) -> str:
        """The text of a News Flash category's archive (its Show Archived)."""
        return self.b.d.execute_async_script(
            "const done = arguments[1]; const b = new URLSearchParams({strResourceType: 'M', lngResourceID: '1', intWhatShown: '2',"
            " ID2: String(arguments[0]), ysnSave: '0', ysnNotifyMe: '0', Scope: '', strPage: 'Archive', intArchCC: '0',"
            " intArchMainCatID: String(arguments[0]), intArchMainItemID: '-1', intArchWhatDisplay: '1',"
            " txtArchFrom: arguments[0] + ',,,CivicAlertItemList'});"
            "fetch('/admin/CivicAlerts.aspx', {method: 'POST', credentials: 'same-origin', body: b}).then(r => r.text())"
            ".then(t => done(new DOMParser().parseFromString(t, 'text/html').body.innerText)).catch(e => done(''))", cid) or ""

    def _all_items(self, mod: str, cid: int) -> list[dict]:
        """Every item of a category. Some lists (Archive Center) show the newest
        50: the public page is read too, and the two put together."""
        items = list(self.relay.items(mod, cid))
        if mod == "archive" and len(items) >= 50:
            html = self.b.d.execute_async_script(
                "const done = arguments[1]; fetch('/Archive.aspx?AMID=' + arguments[0], {credentials: 'same-origin'})"
                ".then(r => r.text()).then(done).catch(e => done(''))", cid)
            seen = {i["id"] for i in items}
            for adid, name in re.findall(r'ADID=(\d+)[^>]*>\s*(?:<[^>]+>\s*)*([^<]{3,120})', html or ""):
                if int(adid) not in seen:
                    seen.add(int(adid))
                    items.append({"id": int(adid), "name": name.strip()})
        return items

    def step_report(self) -> str:
        from .tracking import write_tracking
        p = write_tracking(self.pr, self.work / f"{self.pr.site}-project-tracking.xlsx")
        return str(p)
