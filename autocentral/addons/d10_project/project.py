"""The project: everything the D10 site has, where each piece goes in
Central, and how far it has got. Kept as project.json in the work folder and
written out as the tracking workbook.

Decided here, from the exports (and the old site's menu when the site map
was made):
  pages     the page tree: microsites become section pages, their pages go
            under them; the old menu (site-map.csv) places them when it
            has them. Parents always come before children (Content Import
            reads the sheet top to bottom).
  files     every file and picture the imported content uses, the folder
            it goes in, a name that's unique in the zip
  news      public notices and press releases -> News Flash
  events    pages with dates -> Calendar (made after the import)
  staff     directory listings -> Staff Directory
  jobs      job openings -> Jobs
  archive   ordinances and resolutions -> Archive Center
  business  business directory -> Resource Directory
  tasks     what needs a person
Agenda Center meetings go through the D10 Agenda Center tool's own run.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .content import build_html
from .exports import D10Site, Record, first, media_file, refs

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".tif", ".tiff"}


@dataclass
class Options:
    root_page: str = "Home"                 # the pages go under this page ("Home" = top level)
    docs_root: str = ""                     # the Document Center folder everything goes in
    images_root: str = ""                   # the Public Images folder
    publish_pages: bool = True              # D10-published pages are published after the import
    include_unpublished: bool = False
    zone: str = "Central"                  # the old site's time zone (dates in the exports are UTC)
    agenda_empty_first: bool = False       # delete everything in the Agenda Center before its import

    def defaults(self, site: str) -> "Options":
        nice = site[:1].upper() + site[1:]
        self.docs_root = self.docs_root or f"{nice} Documents"
        self.images_root = self.images_root or f"{nice} Images"
        return self


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date(epoch) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _clean_name(s: str, limit: int = 100) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:limit].rstrip()


def display_name(s: str) -> str:
    """Central names the stored file after the display name, so no path characters."""
    return _clean_name(re.sub(r'[\/:*?"<>|]+', "-", str(s or "")), 200) or "file"


def safe_file_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#%&{}~+]+', "_", name).strip(" .") or "file"
    return name[:120]


# ================================================================ pages
def page_kind(r: Record) -> str:
    f = r.f
    if r.kind == "microsites":
        return "section"
    if refs(f, "field_page_redirect"):
        return "linked"
    if refs(f, "field_news_type"):
        return "news"
    if refs(f, "field_smart_date"):
        return "event"
    return "page"


def read_sitemap(path: Optional[Path]) -> list[dict]:
    """site-map.csv from the migration tool: Tier 1..8, Linked Page, URL."""
    if not path or not Path(path).is_file():
        return []
    rows = list(csv.DictReader(io.StringIO(Path(path).read_text(encoding="utf-8-sig"))))
    out, stack = [], []
    for r in rows:
        tiers = [(i, (r.get(f"Tier {i}") or "").strip()) for i in range(1, 9)]
        tiers = [(i, t) for i, t in tiers if t]
        if not tiers:
            continue
        depth, name = tiers[0]
        stack = stack[:depth - 1] + [name]
        out.append({"depth": depth, "name": name, "path": list(stack), "url": (r.get("URL") or "").strip(),
                    "linked": bool((r.get("Linked Page") or "").strip())})
    return out


def build_pages(site: D10Site, opts: Options, sitemap: list[dict]) -> tuple[list[dict], list[dict]]:
    """The page rows, parents first. Returns (pages, tasks)."""
    tasks: list[dict] = []
    pages: list[dict] = []
    by_key: dict[str, dict] = {}
    root = opts.root_page or "Home"

    def add(key, title, parent_key, kind, rec=None, url="", order=0):
        p = {"key": key, "title": _clean_name(title, 80), "parent": parent_key, "kind": kind,
             "nid": rec.id if rec else "", "old_url": url or (f"https://{site.host}{rec.alias}" if rec and rec.alias else ""),
             "published": rec.published if rec else True, "html": "", "media": [], "nodes": [], "notes": [],
             "link_to": "", "order": order, "state": "planned", "new_id": None, "new_url": "", "detail": ""}
        if rec is not None:
            b = build_html(site, rec)
            p.update(html=b.html, media=b.media, nodes=b.nodes, notes=b.notes)
            if kind == "linked":
                p["link_to"] = re.sub(r"^(internal|entity):", "", str(first(rec.f, "field_page_redirect", "uri") or ""))
                p["link_to"] = re.sub(r"^node/", "/node/", p["link_to"])
                if not p["html"]:
                    p["html"] = f'<p><a href="{p["link_to"]}">{p["title"]}</a></p>'
                    m = site.media_for_url(p["link_to"])
                    if m:
                        p["media"].append(m.id)
                    n = site.node_for_url(p["link_to"])
                    if n:
                        p["nodes"].append(n.id)
        pages.append(p)
        by_key[key] = p
        return p

    if root != "Home":
        add("root", root, "Home", "holder")
        top = "root"
    else:
        top = "Home"
    home_ms = next((m for m in site.of("microsites") if first(m.f, "field_synonym") in ("home-page", "home")
                    or m.title.lower() in ("home page", "home")), None)

    placed: set[str] = set()
    # 1. the old menu
    path_key: dict[tuple, str] = {}
    for i, row in enumerate(sitemap):
        if row["depth"] == 1 and row["name"].lower() == "home":
            continue
        parent_path = tuple(row["path"][:-1])
        parent = path_key.get(parent_path, top)
        node = site.node_for_url(row["url"]) if row["url"] else None
        if node is not None and node.id in placed:
            continue                                  # the same page twice in the menu
        if node is not None and node.kind in ("pages", "microsites") and (node.published or opts.include_unpublished):
            kind = page_kind(node)
            if kind in ("news", "event"):
                kind = "page"                          # it's in the menu, so it's a page
            p = add(f"n{node.id}", row["name"], parent, kind, node, order=i)
            placed.add(node.id)
        elif row["url"] and not node:
            dup = next((q for q in pages if q["parent"] == parent and q["title"] == _clean_name(row["name"], 80)), None)
            if dup:
                continue
            p = add(f"m{i}", row["name"], parent, "linked", order=i)
            p["link_to"] = row["url"]
        elif not row["url"]:
            p = add(f"m{i}", row["name"], parent, "holder", order=i)
        else:
            continue                                   # a meeting, business, ... in the menu: linked later
        path_key[tuple(row["path"])] = p["key"]

    # 2. microsites not in the menu: section pages under the top
    for m in site.of("microsites"):
        if m is home_ms or m.id in placed or not (m.published or opts.include_unpublished):
            continue
        add(f"n{m.id}", m.title, top, "section", m, order=10_000)
        placed.add(m.id)

    # 3. pages not in the menu: under their microsite
    for r in site.of("pages"):
        if r.id in placed or not (r.published or opts.include_unpublished):
            continue
        kind = page_kind(r)
        if kind in ("news", "event"):
            continue
        ms = r.audience
        parent = f"n{ms}" if f"n{ms}" in by_key else top
        add(f"n{r.id}", r.title, parent, kind, r, order=20_000)
        placed.add(r.id)
    if home_ms is not None:
        b = build_html(site, home_ms)
        if b.html:
            tasks.append({"what": "Home page content", "where": "Home",
                          "detail": "The D10 home page's own content (rotators, contact block) belongs in the Central "
                                    "home page layout; it isn't imported as a page.", "state": "to do"})

    # Unique titles: Content Import finds a parent by its name.
    count = Counter(p["title"].lower() for p in pages)
    for p in pages:
        if count[p["title"].lower()] > 1 and any(q["parent"] == p["key"] for q in pages):
            parent = by_key.get(p["parent"])
            p["title"] = _clean_name(f"{p['title']} ({parent['title'] if parent else 'more'})", 80)
            p["notes"].append("renamed: another page has the same name and this one has pages under it")

    # parents before children, menu order within a parent
    ordered, seen = [], set()

    def walk(key):
        for p in sorted((q for q in pages if q["parent"] == key), key=lambda q: q["order"]):
            if p["key"] in seen:
                continue
            seen.add(p["key"])
            ordered.append(p)
            walk(p["key"])
    if "root" in by_key:
        ordered.append(by_key["root"])
        seen.add("root")
    walk(top)
    ordered += [p for p in pages if p["key"] not in seen]
    for p in ordered:
        if p["kind"] == "linked":
            tasks.append({"what": "Linked page", "where": p["title"], "detail": f"links to {p['link_to']}",
                          "state": "to do"})
    return ordered, tasks


# ================================================================ files
def build_files(site: D10Site, opts: Options, uses: dict[str, list[str]]) -> list[dict]:
    """uses: media id -> where it's used (page keys, 'staff:41', ...)."""
    files, names = [], Counter()
    for mid, where in uses.items():
        m = site.media.get(mid)
        if not m:
            continue
        url, name, alt = media_file(m)
        if not url:
            continue
        name = safe_file_name(name)
        stem, dot, ext = name.rpartition(".")
        is_image = m.bundle == "image" or ("." + ext.lower()) in IMAGE_EXT
        ms = site.nodes.get(m.audience)
        folder = _clean_name(ms.title if ms else "General", 60) or "General"
        zip_name = name
        names[name.lower()] += 1
        if names[name.lower()] > 1:
            zip_name = f"{stem}_{mid}.{ext}" if dot else f"{name}_{mid}"
        files.append({"mid": mid, "title": display_name(m.title or name), "url": url, "file": zip_name, "alt": alt or m.title,
                      "image": is_image, "folder": folder,
                      "root": opts.images_root if is_image else opts.docs_root,
                      "used_on": where, "published": m.published,
                      "download": "", "size": 0, "imported": "", "new_id": None, "new_url": ""})
    files.sort(key=lambda f: (f["image"], f["folder"].lower(), f["file"].lower()))
    return files


# ================================================================ other modules
def build_staff(site: D10Site) -> list[dict]:
    out = []
    for r in site.of("directory_listings"):
        if not r.published:
            continue
        f = r.f
        ms = site.nodes.get(r.audience)
        photo = refs(f, "field_primary_image_video") or refs(f, "field_image")
        out.append({"nid": r.id, "first": first(f, "field_first_name"), "last": first(f, "field_last_name") or r.title,
                    "title": first(f, "field_position"), "phone": first(f, "field_phone_number"),
                    "email": first(f, "field_email"), "bio": first(f, "body"),
                    "department": ms.title if ms else "General", "photo": str(photo[0].get("target_id")) if photo else "",
                    "state": "planned"})
    return out


def build_news(site: D10Site) -> list[dict]:
    out = []
    for r in site.of("pages"):
        if page_kind(r) != "news" or not r.published:
            continue
        b = build_html(site, r)
        day = str(first(r.f, "field_news_date") or r.f.get("field_news_date") or "") or _date(first(r.f, "created"))
        if isinstance(r.f.get("field_news_date"), str):
            day = r.f["field_news_date"]
        kind = str(first(r.f, "field_news_type")).replace("_", " ").title()
        out.append({"nid": r.id, "title": r.title, "date": day[:10], "category": kind or "News", "html": b.html,
                    "media": b.media, "nodes": b.nodes, "old_url": f"https://{site.host}{r.alias}", "state": "planned"})
    return out


def build_events(site: D10Site) -> list[dict]:
    out = []
    for r in site.of("pages"):
        if page_kind(r) != "event" or not r.published:
            continue
        d = refs(r.f, "field_smart_date")[0]
        b = build_html(site, r)
        ms = site.nodes.get(r.audience)
        out.append({"nid": r.id, "title": r.title, "start": d.get("value"), "end": d.get("end_value"),
                    "rrule": d.get("rrule") or "", "calendar": ms.title if ms else "Main Calendar", "html": b.html,
                    "old_url": f"https://{site.host}{r.alias}", "state": "planned"})
    return out


def build_jobs(site: D10Site) -> list[dict]:
    out = []
    for r in site.of("jobs"):
        if not r.published:
            continue
        b = build_html(site, r)
        ms = site.nodes.get(r.audience)
        out.append({"nid": r.id, "title": r.title, "category": ms.title if ms else "Jobs", "html": b.html,
                    "start": _date(first(r.f, "created")) or datetime.now().strftime("%Y-%m-%d"),
                    "media": b.media, "nodes": b.nodes, "state": "planned"})
    return out


def build_archive(site: D10Site) -> list[dict]:
    out = []
    for r in site.of("ordinances"):
        if not r.published:
            continue
        f = r.f
        kind = "Resolutions" if str(first(f, "field_ordinance_resolution")) == "2" or "resolution" in r.title.lower() \
            else "Ordinances"
        att = [str(a.get("target_id")) for a in refs(f, "field_file_attachments")]
        day = f.get("field_adopted_date") if isinstance(f.get("field_adopted_date"), str) else first(f, "field_adopted_date")
        body = re.sub(r"<[^>]+>", " ", str(first(f, "body") or ""))
        out.append({"nid": r.id, "title": r.title, "category": kind, "date": str(day or "")[:10],
                    "description": re.sub(r"\s+", " ", body).strip()[:500], "media": att,
                    "old_url": f"https://{site.host}{r.alias}", "state": "planned"})
    return out


def build_business(site: D10Site) -> list[dict]:
    out = []
    for r in site.of("business_directory"):
        if not r.published:
            continue
        f = r.f
        a = (refs(f, "field_address") or [{}])[0]
        cats = [site.term(c.get("target_id")) for c in refs(f, "field_business_category")]
        out.append({"nid": r.id, "name": r.title, "address": a.get("address_line1", ""), "address2": a.get("address_line2", ""),
                    "city": a.get("locality", ""), "state_code": a.get("administrative_area", ""), "zip": a.get("postal_code", ""),
                    "phone": first(f, "field_phone_number"), "email": first(f, "field_email"),
                    "web": first(f, "field_website_link", "uri"), "categories": [c for c in cats if c],
                    "description": first(f, "body"),
                    "photo": str((refs(f, "field_primary_image_video") or [{}])[0].get("target_id") or ""),
                    "state": "planned"})
    return out


# ================================================================ the project
@dataclass
class Project:
    work: Path
    site: str = ""
    old_host: str = ""
    exports: str = ""
    options: dict = field(default_factory=dict)
    steps: dict = field(default_factory=dict)          # key -> {"state", "at", "note"}
    pages: list = field(default_factory=list)
    files: list = field(default_factory=list)
    news: list = field(default_factory=list)
    events: list = field(default_factory=list)
    staff: list = field(default_factory=list)
    jobs: list = field(default_factory=list)
    archive: list = field(default_factory=list)
    business: list = field(default_factory=list)
    links: list = field(default_factory=list)          # {"where", "old", "new", "kind", "state"}
    tasks: list = field(default_factory=list)
    central: str = ""                                  # the Central site it's going to
    before_pages: list = field(default_factory=list)
    check: list = field(default_factory=list)          # the last check: {"module", "item", "issue", "detail"}   # page ids on the site before the content import
    log: list = field(default_factory=list)

    @property
    def path(self) -> Path:
        return Path(self.work) / "project.json"

    def save(self) -> None:
        Path(self.work).mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        d["work"] = str(self.work)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.path)

    @classmethod
    def load(cls, work: Path) -> Optional["Project"]:
        p = Path(work) / "project.json"
        if not p.is_file():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        d["work"] = Path(d.get("work") or work)
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    def mark(self, step: str, state: str, note: str = "") -> None:
        self.steps[step] = {"state": state, "at": now(), "note": note}
        self.save()

    def file(self, mid: str) -> Optional[dict]:
        return next((f for f in self.files if f["mid"] == str(mid)), None)

    def page(self, key: str) -> Optional[dict]:
        return next((p for p in self.pages if p["key"] == key), None)

    def page_for_nid(self, nid: str) -> Optional[dict]:
        return next((p for p in self.pages if p["nid"] == str(nid)), None)

    def counts(self) -> dict:
        return {"pages": len(self.pages), "files": len(self.files), "news": len(self.news),
                "events": len(self.events), "staff": len(self.staff), "jobs": len(self.jobs),
                "archive": len(self.archive), "business": len(self.business), "tasks": len(self.tasks)}


def default_work(exports: Path) -> Path:
    return Path(exports) / "central-project"


def plan(site: D10Site, work: Path, opts: Options, sitemap_csv: Optional[Path] = None) -> Project:
    """Build the project from the exports (nothing on any site changes)."""
    opts.defaults(site.name)
    pr = Project(work=Path(work), site=site.name, old_host=site.host, exports=str(site.folder), options=asdict(opts))
    pr.pages, pr.tasks = build_pages(site, opts, read_sitemap(sitemap_csv))
    pr.news, pr.events, pr.staff = build_news(site), build_events(site), build_staff(site)
    pr.jobs, pr.archive, pr.business = build_jobs(site), build_archive(site), build_business(site)
    uses: dict[str, list[str]] = {}
    for p in pr.pages:
        for mid in p["media"]:
            uses.setdefault(mid, []).append(p["title"])
    for kind in ("news", "jobs"):
        for r in getattr(pr, kind):
            for mid in r["media"]:
                uses.setdefault(mid, []).append(f"{kind}: {r['title']}")
    for s in pr.staff:
        if s["photo"]:
            uses.setdefault(s["photo"], []).append(f"staff: {s['first']} {s['last']}")
    for b in pr.business:
        if b["photo"]:
            uses.setdefault(b["photo"], []).append(f"directory: {b['name']}")
    pr.files = build_files(site, opts, uses)
    archive_mids = {m for a in pr.archive for m in a["media"]}
    for mid in archive_mids:                     # the Archive Center files travel with their rows
        m = site.media.get(mid)
        if m and not pr.file(mid):
            url, name, alt = media_file(m)
            if url:
                pr.files.append({"mid": mid, "title": m.title or name, "url": url, "file": safe_file_name(name),
                                 "alt": alt, "image": False, "folder": "", "root": "Archive Center",
                                 "used_on": ["archive"], "published": m.published, "download": "", "size": 0,
                                 "imported": "", "new_id": None, "new_url": ""})
    for e in pr.events:
        pr.tasks.append({"what": "Calendar event", "where": e["calendar"], "detail": e["title"], "state": "to do"})
    pr.steps["plan"] = {"state": "done", "at": now(), "note": "; ".join(f"{v} {k}" for k, v in pr.counts().items())}
    pr.save()
    return pr
