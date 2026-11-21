"""Read a D10 site's JSON exports (www-pages.json, www-media.json, ...).

Each export is a list of records; a record's "fields" is the Drupal entity's
fields, written as a Python literal (single quotes, True/None), so it's read
with ast.literal_eval rather than json.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

KINDS = ["pages", "microsites", "paragraphs", "media", "directory_listings", "business_directory", "jobs",
         "ordinances", "meetings", "taxonomy_terms", "faqs", "bids_rfps"]


def _fields(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return ast.literal_eval(raw) if raw else {}
    except (ValueError, SyntaxError):
        try:
            return json.loads(raw)
        except ValueError:
            return {}


def first(f: dict, key: str, sub: str = "value", default: Any = "") -> Any:
    """The first value of a field: first(f, 'title') -> 'Lost Animals'."""
    v = f.get(key)
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get(sub)
    return default if v is None else v


def refs(f: dict, key: str) -> list[dict]:
    v = f.get(key)
    return v if isinstance(v, list) else []


@dataclass
class Record:
    kind: str                 # the export it came from: pages, microsites, media, ...
    id: str                   # nid / mid / paragraph id / tid
    bundle: str
    published: bool
    f: dict

    @property
    def title(self) -> str:
        return str(first(self.f, "title") or first(self.f, "name") or "").strip()

    @property
    def alias(self) -> str:
        return str(first(self.f, "path", "alias") or "")

    @property
    def audience(self) -> str:
        """The microsite (nid) the record belongs to."""
        return str(first(self.f, "og_audience", "target_id") or first(self.f, "microsite", "target_id") or "")


@dataclass
class D10Site:
    folder: Path
    records: dict[str, list[Record]] = field(default_factory=dict)
    nodes: dict[str, Record] = field(default_factory=dict)        # nid -> node of any kind
    media: dict[str, Record] = field(default_factory=dict)        # mid -> media
    paragraphs: dict[str, Record] = field(default_factory=dict)
    terms: dict[str, Record] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)         # '/police/page/x' -> nid
    host: str = ""                                                # www.cityofhiawatha.org
    name: str = ""                                                # cityofhiawatha

    def of(self, kind: str) -> list[Record]:
        return self.records.get(kind, [])

    def node_for_url(self, url: str) -> Optional[Record]:
        """The node an old-site address points at (/node/31, /police/page/x, full URL)."""
        if not url:
            return None
        url = re.sub(r"^(internal|entity):", "", url)
        p = urlparse(url)
        if p.netloc and self.host and p.netloc.lower().replace("www.", "") != self.host.replace("www.", ""):
            return None
        path = (p.path or "/").rstrip("/") or "/"
        m = re.fullmatch(r"/node/(\d+)", path)
        if m:
            return self.nodes.get(m.group(1))
        nid = self.aliases.get(path.lower())
        return self.nodes.get(nid) if nid else None

    def media_for_url(self, url: str) -> Optional[Record]:
        url = re.sub(r"^(internal|entity):", "", url or "")
        p = urlparse(url)
        m = re.fullmatch(r"/media/(\d+)(?:/download)?", (p.path or "").rstrip("/"))
        if m:
            return self.media.get(m.group(1))
        return self._by_file_url.get(_norm_file(url)) if url else None

    def term(self, tid: Any) -> str:
        t = self.terms.get(str(tid))
        return t.title if t else ""


def _norm_file(url: str) -> str:
    p = urlparse(url)
    return (p.path or "").lower()


def media_file(m: Record) -> tuple[str, str, str]:
    """(file URL, file name, alt text) of a media record ('' when it has none)."""
    for key in ("field_media_file", "field_media_image", "field_media_file_1", "field_media_document"):
        v = refs(m.f, key)
        if v and v[0].get("file_url"):
            url = v[0]["file_url"]
            name = v[0].get("file_name") or Path(urlparse(url).path).name
            return url, name, str(v[0].get("alt") or "")
    return "", "", ""


def load(folder: Path) -> D10Site:
    folder = Path(folder)
    site = D10Site(folder)
    for p in sorted(folder.glob("*.json")):
        m = re.match(r"^(?:[a-z0-9]+-)?([a-z_]+)\.json$", p.name)
        if not m or m.group(1) not in KINDS:
            continue
        kind = m.group(1)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        recs = []
        for r in data if isinstance(data, list) else data.get("records", []):
            f = _fields(r.get("fields"))
            pub = str(r.get("is_published", first(f, "status"))).lower() in ("true", "1")
            recs.append(Record(kind, str(r.get("entity_id", "")), str(r.get("bundle", "")), pub, f))
        site.records[kind] = recs
    for kind, recs in site.records.items():
        for r in recs:
            if kind == "media":
                site.media[r.id] = r
            elif kind == "paragraphs":
                site.paragraphs[r.id] = r
            elif kind == "taxonomy_terms":
                site.terms[r.id] = r
            else:
                site.nodes[r.id] = r
                if r.alias:
                    site.aliases[r.alias.rstrip("/").lower()] = r.id
    site._by_file_url = {}
    for m in site.media.values():
        url = media_file(m)[0]
        if url:
            site._by_file_url[_norm_file(url)] = m
    # The old site's address, from the canonical links in the metatags.
    for r in site.of("pages") + site.of("microsites"):
        for t in refs(r.f, "metatag"):
            href = (t.get("attributes") or {}).get("href", "")
            if (t.get("attributes") or {}).get("rel") == "canonical" and href.startswith("http"):
                site.host = urlparse(href).netloc.lower()
                break
        if site.host:
            break
    if not site.host:
        for m in site.media.values():
            url = media_file(m)[0]
            if url:
                site.host = urlparse(url).netloc.lower()
                break
    site.name = re.sub(r"^www\.", "", site.host).split(".")[0] if site.host else folder.name
    return site
