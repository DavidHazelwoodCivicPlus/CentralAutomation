"""Pages and Document Center: turning what relay.js reads into table
rows, matching the files and images pages use to Document Center / Public
Images, and the site map export.

Nothing here talks to the site; scanner.py reads, this shapes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .models import InventoryRow, ModuleScan

# Link kinds counted on a page row, in column order (phone numbers aren't listed).
LINK_COLUMNS = (("page", "Page links"), ("module", "Module links"), ("file", "Files"), ("image file", "Image files"),
                ("external", "External links"), ("site link", "Other site links"), ("email", "Email"),
                ("broken", "Broken"))
FILE_KINDS = ("file", "image file", "image")

_DOC_RE = re.compile(r"/DocumentCenter/(?:Home/)?View/(\d+)", re.I)


def page_kind(slug: str, title: str) -> str:
    if re.search(r"Layout-Page$", slug or "") or "Layout Page" in (title or ""):
        return "layout page"
    if re.match(r"CustomLinks", slug or ""):
        return "custom links"
    return "page"


def doc_id_of(href: str) -> Optional[int]:
    """The Document Center / Public Images document a link or image points at."""
    m = _DOC_RE.search(href or "")
    if m:
        return int(m.group(1))
    u = urlparse(href or "")
    if "/imagerepository/" in u.path.lower():
        v = parse_qs(u.query).get("documentID") or parse_qs(u.query).get("documentid")
        if v and v[0].isdigit():
            return int(v[0])
    return None


def file_name_of(href: str) -> str:
    """A file's name from its address (replaced by the Document Center name
    when the document is found there)."""
    u = urlparse(href or "")
    m = _DOC_RE.search(u.path)
    if m:
        rest = u.path[m.end():].strip("/")
        return unquote(rest) or f"document {m.group(1)}"
    if "/imagerepository/" in u.path.lower():
        return f"image {doc_id_of(href) or ''}".strip()
    return unquote(u.path.rsplit("/", 1)[-1]) or href


def is_internal(href: str, site_host: str = "") -> bool:
    u = urlparse(href or "")
    return not u.netloc or (site_host and u.netloc.lower() == site_host.lower())


# ===================================================================== pages
def add_page_rows(ms: ModuleScan, pg: dict, slug: str, site_host: str = "") -> None:
    """One page (as relay.js reads it) -> a page row and its link / widget / image rows."""
    pid = int(pg["id"])
    title = pg.get("title") or slug.replace("-", " ") or str(pid)
    if pid == 1 and not pg.get("path"):
        title = "Home"
    links = [ln for ln in (pg.get("links") or []) if ln.get("type") != "phone"]
    counts = {k: 0 for k, _ in LINK_COLUMNS}
    for ln in links:
        counts[ln["type"]] = counts.get(ln["type"], 0) + 1
    widgets = pg.get("widgets") or []
    images = pg.get("images") or []
    path = pg.get("path") or []
    fields = {"URL": f"/{pid}/{slug}", "Kind": page_kind(slug, title), "Parent": " > ".join(path)}
    if pg.get("linkTo"):
        fields["Links to"] = pg["linkTo"]
        fields["Kind"] = "linked page (" + (pg.get("linkToType") or "link") + ")"
    fields["Widgets"] = ", ".join(w["type"] + (f" ({w['title']})" if w.get("title") else "") for w in widgets)
    fields["Images"] = str(len(images)) if images else ""
    for k, label in LINK_COLUMNS:
        fields[label] = str(counts.get(k, 0)) if counts.get(k) else ""
    hosts = sorted({ln["to"] for ln in links if ln["type"] == "external" and ln.get("to")})
    summary = [("links to " + pg["linkTo"]) if pg.get("linkTo") else "",
               f"{len(widgets)} widget(s)" if widgets else "", f"{len(links)} link(s)" if links else "",
               f"{len(images)} image(s)" if images else "", ("links out to " + ", ".join(hosts[:4])) if hosts else "",
               pg.get("error") or ""]
    ms.rows.append(InventoryRow(ms.module, "category", " > ".join(path + [title]), title,
                                pg.get("status") or "unknown", category_id=pid,
                                detail="; ".join(x for x in summary if x),
                                extra={"parent_id": pg.get("parent"), "version": pg.get("version")}, fields=fields))
    for b in pg.get("blocks") or []:
        text = b.get("text") or ""
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        words = len(text.split())
        ms.rows.append(InventoryRow(ms.module, "item", title, (first[:90] + ("…" if len(first) > 90 else "")) or "(empty text block)",
                                    "text block", category_id=pid,
                                    detail=f"{words} word(s), {b.get('links', 0)} link(s), {b.get('images', 0)} image(s)",
                                    extra={"block_id": b.get("id", "")},
                                    fields={"Kind": "text block", "Words": str(words), "Text": text, "HTML": b.get("html") or ""}))
    for w in widgets:
        ms.rows.append(InventoryRow(ms.module, "item", title, w.get("title") or w["type"], "widget",
                                    category_id=pid, detail=f"{w['type']} widget",
                                    fields={"Kind": f"widget: {w['type']}"}))
    for ln in links:
        kind = ln["type"] + (f": {ln['to']}" if ln.get("to") else "")
        f = {"Kind": kind, "URL": ln["href"], "Shown as": ln.get("text") or "",
             "Internal / external": "internal" if is_internal(ln["href"], site_host) else "external"}
        if ln["type"] in FILE_KINDS:
            f["File name"] = file_name_of(ln["href"])
        ms.rows.append(InventoryRow(ms.module, "item", title, ln.get("text") or ln["href"], ln["type"],
                                    category_id=pid,
                                    detail=ln["href"] + (f" (in the {ln['widget']} widget)" if ln.get("widget") else ""),
                                    fields=f))
    for im in images:
        ms.rows.append(InventoryRow(ms.module, "item", title, im.get("alt") or file_name_of(im["src"]), "image",
                                    category_id=pid, detail=im["src"] + ("" if im.get("alt") else " (no alt text)"),
                                    fields={"Kind": "image", "URL": im["src"], "Shown as": im.get("alt") or "(no alt text)",
                                            "File name": file_name_of(im["src"]),
                                            "Internal / external": "internal" if is_internal(im["src"], site_host)
                                            else "external"}))


def mark_shared_blocks(ms: ModuleScan) -> None:
    """A text block that shows on several pages (a shared or inherited area)
    says so, so its text isn't mistaken for each page's own."""
    count: dict[str, int] = {}
    for r in ms.rows:
        bid = r.extra.get("block_id") if r.status == "text block" else None
        if bid:
            count[bid] = count.get(bid, 0) + 1
    for r in ms.rows:
        bid = r.extra.get("block_id") if r.status == "text block" else None
        if bid and count[bid] > 1:
            r.fields["Shared"] = f"on {count[bid]} pages"


PREVIEW_NOTE = ("Preview made by AutoCentral from this page's text blocks with the site's stylesheets. "
                "It is close to the live page, not the live page itself: menus, other widgets and the "
                "page layout are left out.")


def preview_html(site: str, title: str, blocks: list[str], stylesheets: list[str]) -> str:
    """A stand-alone page showing text blocks the way the site styles them."""
    import html as _h
    base = site.rstrip("/") + "/"
    links = "\n".join(f'<link rel="stylesheet" href="{_h.escape(u)}">' for u in stylesheets)
    body = "\n".join(f'<div class="widget editor">{b}</div>' for b in blocks) or "<p>(no text blocks)</p>"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><base href="{_h.escape(base)}"><title>Preview: {_h.escape(title)}</title>
{links}
<style>.acNote{{font:13px Segoe UI,Arial,sans-serif;background:#fff3cd;border:1px solid #e0c36c;padding:8px 12px;margin:0 0 16px}}
#acPreview{{max-width:1100px;margin:20px auto;padding:0 16px;background:#fff}}</style></head>
<body><div id="acPreview"><p class="acNote">{_h.escape(PREVIEW_NOTE)}</p>
<div id="page"><div class="contentWrap"><h1>{_h.escape(title)}</h1>
{body}
</div></div></div></body></html>"""


def preview_pages_html(site: str, pages: list[dict], stylesheets: list[str]) -> str:
    """Many pages in one preview: one shown at a time, ← / → (or the buttons)
    to step through, with each page's status and address at the top."""
    import html as _h
    import json as _json
    base = site.rstrip("/") + "/"
    links = "\n".join(f'<link rel="stylesheet" href="{_h.escape(u)}">' for u in stylesheets)
    data = _json.dumps(pages).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><base href="{_h.escape(base)}"><title>Page previews</title>
{links}
<style>#acBar{{position:sticky;top:0;z-index:99;font:13px Segoe UI,Arial,sans-serif;background:#1f4e79;color:#fff;padding:8px 12px;display:flex;gap:12px;align-items:center}}
#acBar button{{font:inherit;padding:4px 10px}} #acBar a{{color:#cfe3ff}} .acNote{{font:12px Segoe UI,Arial,sans-serif;color:#666;margin:6px 0 14px}}
#acPreview{{max-width:1100px;margin:16px auto;padding:0 16px;background:#fff}}</style></head>
<body><div id="acBar"><button id="acPrev">&#9664; Previous</button><button id="acNext">Next &#9654;</button>
<span id="acPos"></span><strong id="acTitle"></strong><span id="acStatus"></span><a id="acLink" target="_blank">open the live page</a></div>
<div id="acPreview"><p class="acNote">{_h.escape(PREVIEW_NOTE)} Use the &#8592; and &#8594; keys to step through the pages.</p>
<div id="page"><div class="contentWrap"><div id="acPath" class="acNote"></div><h1 id="acH1"></h1><div id="acBody"></div></div></div></div>
<script>
const P = {data}; let i = 0;
function show(k) {{
  i = Math.max(0, Math.min(P.length - 1, k)); const p = P[i];
  document.getElementById('acPos').textContent = (i + 1) + ' of ' + P.length;
  document.getElementById('acTitle').textContent = p.title;
  document.getElementById('acStatus').textContent = '(' + p.status + ')';
  document.getElementById('acH1').textContent = p.title;
  document.getElementById('acPath').textContent = p.path ? 'In: ' + p.path : '';
  const a = document.getElementById('acLink'); a.href = p.url; a.style.display = p.url ? '' : 'none';
  document.getElementById('acBody').innerHTML = p.blocks.length ? p.blocks.map(b => '<div class="widget editor">' + b + '</div>').join('') : '<p><em>(no text blocks)</em></p>';
  window.scrollTo(0, 0);
}}
document.getElementById('acPrev').onclick = () => show(i - 1);
document.getElementById('acNext').onclick = () => show(i + 1);
document.addEventListener('keydown', e => {{ if (e.key === 'ArrowLeft') show(i - 1); if (e.key === 'ArrowRight') show(i + 1); }});
show(0);
</script></body></html>"""


# ======================================================= documents and images
def add_doc_rows(ms: ModuleScan, root: dict, folders: list[dict], files: list[dict]) -> None:
    """Document Center or Public Images: the root, its folders (nested) and
    every file in them."""
    where = root["name"]
    ms.rows.append(InventoryRow(ms.module, "category", where, where, "", category_id=int(root["id"]),
                                fields={"Where": where}))
    names = {int(root["id"]): where}
    for f in folders:
        names[int(f["id"])] = f["name"]
    for f in folders:
        path = [where] + list(f.get("path") or [])
        ms.rows.append(InventoryRow(ms.module, "category", " > ".join(path + [f["name"]]), f["name"], "folder",
                                    category_id=int(f["id"]), extra={"parent_id": int(f["parent"])},
                                    fields={"Where": where, "Folder": " > ".join(path)}))
    for d in files:
        ms.rows.append(InventoryRow(ms.module, "item", names.get(int(d["folder"]), ""), d.get("name") or d.get("file") or "",
                                    (d.get("status") or "").lower() or "unknown",
                                    category_id=int(d["folder"]), item_id=int(d["id"]),
                                    modified_on=d.get("modified") or "",
                                    detail=("archived; " if d.get("archived") else "") + (d.get("link") or ""),
                                    fields={"Where": where, "File name": d.get("file") or "", "Type": (d.get("type") or "").lstrip("."),
                                            "URL": d.get("link") or "", "Central's reference count": str(d.get("refs") or 0)}))


def cross_reference(pages: Optional[ModuleScan], docs: Optional[ModuleScan]) -> None:
    """Match the files and images on pages to Document Center / Public Images:
    page rows get the document's real file name, document rows get the pages
    they appear on. Safe to run again after either is read."""
    if not pages or not docs:
        return
    by_id = {r.item_id: r for r in docs.rows if r.level == "item" and r.item_id is not None}
    used: dict[int, list[str]] = {}
    for r in pages.rows:
        if r.level != "item" or r.status not in FILE_KINDS:
            continue
        did = doc_id_of(r.fields.get("URL", ""))
        doc = by_id.get(did) if did else None
        if doc is not None:
            r.fields["File name"] = doc.fields.get("File name") or doc.name
            r.fields["In Document Center"] = f"{doc.fields.get('Where')}: {doc.category} (ID {did})"
            used.setdefault(did, [])
            if r.category not in used[did]:
                used[did].append(r.category)
        elif did and r.fields.get("Internal / external") == "internal":
            r.fields["In Document Center"] = f"not found (ID {did})"
    for did, doc in by_id.items():
        on = used.get(did, [])
        doc.fields["Used on pages"] = str(len(on)) if on else "0"
        doc.fields["Pages"] = "; ".join(on[:20]) + (f" … and {len(on) - 20} more" if len(on) > 20 else "")


# ============================================================ site map export
def export_sitemap(path: Path, site: str, rows: list[InventoryRow], depth_of: dict[int, int]) -> Path:
    """Pages as a CivicPlus-style site map: each page in the column of its
    tier (Home in column A, its sub-pages in B ...), in menu order, with its
    status, address and what's on it."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    pages = [r for r in rows if r.level == "category"]
    tiers = max([depth_of.get(id(r), 0) for r in pages] + [0]) + 1
    wb = Workbook()
    ws = wb.active
    ws.title = "Site Map"
    extra = ["Status", "Kind", "URL", "Links to", "Widgets", "Page links", "Module links", "Files", "Image files",
             "Images", "External links", "Broken links", "Page ID"]
    head = [f"Tier {i + 1}" for i in range(tiers)] + extra
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E79")
    tier_fill = [PatternFill("solid", fgColor=x) for x in ("D9E1F2", "E2EFDA", "FFF2CC", "FCE4D6", "EDEDED")]
    base = site.rstrip("/")
    for r in pages:
        t = depth_of.get(id(r), 0)
        line = [""] * tiers
        line[t] = r.name
        f = r.fields
        url = f.get("URL", "")
        ws.append(line + [r.status, f.get("Kind", ""), base + url if url.startswith("/") else url, f.get("Links to", ""),
                          f.get("Widgets", ""), f.get("Page links", ""), f.get("Module links", ""), f.get("Files", ""),
                          f.get("Image files", ""), f.get("Images", ""), f.get("External links", ""),
                          f.get("Broken links", ""), r.category_id])
        row = ws.max_row
        cell = ws.cell(row=row, column=t + 1)
        cell.font = Font(bold=(t == 0))
        cell.fill = tier_fill[min(t, len(tier_fill) - 1)]
        if r.status not in ("published", ""):
            cell.font = Font(italic=True, color="A15C00", bold=(t == 0))
        link = ws.cell(row=row, column=tiers + 3)
        if link.value:
            link.hyperlink = link.value
            link.font = Font(color="1F5FA8", underline="single")
    for i in range(tiers):
        ws.column_dimensions[get_column_letter(i + 1)].width = 28
    for i, w in enumerate((12, 16, 50, 40, 30), start=tiers + 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top")
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def tree_depths(rows: list[InventoryRow], parent_of: dict[int, Optional[int]]) -> dict[int, int]:
    """id(row) -> how many category rows are above it (its tier)."""
    out = {}
    for i, r in enumerate(rows):
        d, p = 0, parent_of.get(i)
        while p is not None and d < 50:
            if rows[p].level == "category":
                d += 1
            p = parent_of.get(p)
        out[id(r)] = d
    return out


def unique_urls(rows: Iterable[InventoryRow]) -> list[str]:
    seen, out = set(), []
    for r in rows:
        u = r.fields.get("URL", "") if r.level == "item" else ""
        if u and not u.lower().startswith(("mailto:", "tel:")) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ============================================================ staff directory
def split_name(tree_name: str) -> tuple[str, str]:
    """The department tree names people "Last, First": -> (first, last)."""
    last, sep, first = (tree_name or "").partition(",")
    if not sep:
        parts = (tree_name or "").split()
        return (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else ("", tree_name or "")
    return first.strip(), last.strip()


def staff_name(first: str, last: str, last_first: bool = True) -> str:
    if last_first:
        return ", ".join(x for x in (last, first) if x) or "(no name)"
    return " ".join(x for x in (first, last) if x) or "(no name)"


def set_staff_name_order(ms: Optional[ModuleScan], last_first: bool) -> None:
    """Show employees as "Last, First" or "First Last" (rows keep both parts)."""
    if not ms or ms.module != "staff":
        return
    for r in ms.rows:
        if r.level == "item" and ("first" in r.extra or "last" in r.extra):
            r.name = staff_name(r.extra.get("first", ""), r.extra.get("last", ""), last_first)


def add_staff_rows(ms: ModuleScan, tree: list[dict], people: list[dict], export: list[dict],
                   last_first: bool = True) -> None:
    """Employees as the department tree lists them (id, status), with their
    title, email and phone from Export Staff where the name and department match."""
    def norm(x: str) -> str:
        return re.sub(r"\s+", " ", (x or "")).strip().lower()
    names = {d["id"]: d["name"] for d in tree}
    paths = {d["id"]: " > ".join(list(d.get("path") or []) + [d["name"]]) for d in tree}
    # Export Staff gives each person's department as a path: Department1 (top
    # level), Department2 (the department inside it) ... Match on the whole
    # path, else on the deepest department's name.
    details: dict[tuple[str, str], dict] = {}
    for e in export:
        first = e.get("First Name") or e.get("FirstName") or ""
        last = e.get("Last Name") or e.get("LastName") or ""
        levels = [e[k] for k in sorted((k for k in e if re.match(r"department\d*$", k.lower())),
                                       key=lambda k: int(re.sub(r"\D", "", k) or 0)) if e[k]]
        who = norm(f"{first} {last}")
        if levels:
            details.setdefault((norm(" > ".join(levels)), who), e)
            details.setdefault((norm(levels[-1]), who), e)
    for p in people:
        first, last = split_name(p.get("name", ""))
        dept = names.get(int(p["dept"]), "")
        who = norm(f"{first} {last}")
        e = details.get((norm(paths.get(int(p["dept"]), "")), who)) or details.get((norm(dept), who), {})
        title = e.get("Title") or e.get("Job Title") or ""
        email = e.get("Email", "")
        status = "published" if p.get("published") is True else "unpublished" if p.get("published") is False else ""
        ms.rows.append(InventoryRow(ms.module, "item", dept, staff_name(first, last, last_first), status,
                                    category_id=int(p["dept"]), item_id=int(p["id"]),
                                    detail="; ".join(x for x in (title, email) if x),
                                    extra={"first": first, "last": last},
                                    fields={"Title": title, "Email": email,
                                            "Phone": e.get("Phone") or e.get("Work Phone") or ""}))
