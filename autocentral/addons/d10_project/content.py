"""Turn a D10 page or microsite into the HTML of one Central Editor widget,
and rewrite the addresses in it.

A D10 page is its body plus "sections" (paragraphs: text, image and text,
slideshows, embedded staff or pages, cards), its attachments and its main
picture. Central gets them as one Editor block, in that order. Slideshows
become their pictures in a row, embedded staff a short list, embedded pages
a list of links; each such conversion is noted so someone can swap in the
right Central widget later.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .exports import D10Site, Record, first, media_file, refs

ATTR = re.compile(r'(\b(?:href|src)\s*=\s*)(["\'])(.*?)\2', re.I | re.S)


@dataclass
class Built:
    html: str
    media: list[str] = field(default_factory=list)    # media ids the HTML uses
    nodes: list[str] = field(default_factory=list)    # node ids it links to
    notes: list[str] = field(default_factory=list)    # what needs a person or a widget


def _e(s: str) -> str:
    return _html.escape(str(s or ""), quote=True)


def _media_link(site: D10Site, mid: str, out: Built, label: str = "") -> str:
    m = site.media.get(str(mid))
    if not m:
        return ""
    url, name, alt = media_file(m)
    if not url:
        return ""
    out.media.append(m.id)
    if m.bundle == "image":
        return f'<img src="/media/{m.id}" alt="{_e(alt or m.title)}" style="max-width:100%;height:auto;">'
    return f'<a href="/media/{m.id}">{_e(label or m.title or name)}</a>'


def _paragraph(site: D10Site, p: Record, out: Built) -> str:
    f, b = p.f, p.bundle
    head = first(f, "field_embed_header") or first(f, "field_title")
    level = first(f, "field_header_type") or "h2"
    level = level if re.fullmatch(r"h[2-6]", str(level)) else "h2"
    h = f"<{level}>{_e(head)}</{level}>" if head and b != "card" else ""
    text = first(f, "field_text")
    if b == "image_text":
        img = "".join(_media_link(site, r.get("target_id"), out) for r in refs(f, "field_image"))
        side = "right" if first(f, "field_position") == "right" else "left"
        img = f'<div style="float:{side};max-width:40%;margin:0 1rem 1rem 0;">{img}</div>' if img else ""
        return h + img + (text or "") + '<div style="clear:both"></div>'
    if b in ("text", "custom_text"):
        return h + (text or "")
    if b == "slideshow":
        imgs = [_media_link(site, r.get("target_id"), out) for r in refs(f, "field_images")]
        out.notes.append("slideshow made into a row of pictures (a Slideshow widget may suit better)")
        return h + "<p>" + " ".join(i for i in imgs if i) + "</p>"
    if b == "embedded_directory_listings":
        people = []
        for r in refs(f, "field_contact_info_individual"):
            n = site.nodes.get(str(r.get("target_id")))
            if n:
                pos = first(n.f, "field_position")
                phone = first(n.f, "field_phone_number")
                mail = first(n.f, "field_email")
                bits = [f"<strong>{_e(n.title)}</strong>"] + [_e(x) for x in (pos, phone) if x]
                if mail:
                    bits.append(f'<a href="mailto:{_e(mail)}">{_e(mail)}</a>')
                people.append("<li>" + "<br>".join(bits) + "</li>")
        for r in refs(f, "field_contact_info_department"):
            n = site.nodes.get(str(r.get("target_id")))
            if n and not people:
                people.append(f"<li>{_e(n.title)} staff</li>")
        out.notes.append("embedded staff listing made into a list (a Staff Directory widget may suit better)")
        return h + ("<ul>" + "".join(people) + "</ul>" if people else "")
    if b in ("embedded_pages", "card"):
        links = []
        for key in ("field_embed_individual", "field_pages_to_load"):
            for r in refs(f, key):
                n = site.nodes.get(str(r.get("target_id")))
                if n:
                    out.nodes.append(n.id)
                    links.append(f'<li><a href="/node/{n.id}">{_e(n.title)}</a></li>')
        if links:
            out.notes.append(f"{b.replace('_', ' ')} made into a list of links")
        return h + ("<ul>" + "".join(links) + "</ul>" if links else "")
    if b == "social_media":
        link = first(f, "field_social_media_link", "uri")
        return f'<p><a href="{_e(link)}">{_e(link)}</a></p>' if link else ""
    out.notes.append(f"section type '{b}' left out")
    return ""


def _sections(site: D10Site, rec: Record, key: str, out: Built) -> list[str]:
    parts = []
    for r in refs(rec.f, key):
        p = site.paragraphs.get(str(r.get("target_id")))
        if p is None and isinstance(r.get("entity"), dict):            # paragraphs carried inline
            e = r["entity"]
            f = e.get("fields") or {}
            p = Record("paragraphs", str(e.get("entity_id")), str(e.get("bundle")), True, f if isinstance(f, dict) else {})
        if p is not None and p.published:
            parts.append(_paragraph(site, p, out))
    return parts


def _contact(rec: Record) -> str:
    f = rec.f
    lines = []
    a = refs(f, "field_address")
    if a:
        a = a[0]
        street = " ".join(x for x in (a.get("address_line1"), a.get("address_line2")) if x)
        town = ", ".join(x for x in (a.get("locality"), " ".join(y for y in (a.get("administrative_area"), a.get("postal_code")) if y)) if x)
        lines += [x for x in (street, town) if x]
    for key, label in (("field_phone_number", "Phone"), ("field_fax_number", "Fax")):
        v = first(f, key)
        if v:
            lines.append(f"{label}: {_e(v)}")
    mail = first(f, "field_email")
    if mail:
        lines.append(f'<a href="mailto:{_e(mail)}">{_e(mail)}</a>')
    hours = first(f, "field_hours")
    out = ("<p>" + "<br>".join(lines) + "</p>") if lines else ""
    if hours:
        out += "<h3>Hours</h3>" + (hours if "<" in str(hours) else f"<p>{_e(hours)}</p>")
    return out


def build_html(site: D10Site, rec: Record) -> Built:
    """The Editor block for a page or microsite."""
    out = Built("")
    parts = []
    prim = refs(rec.f, "field_primary_image_video")
    if prim:
        img = _media_link(site, prim[0].get("target_id"), out)
        if img:
            side = "right" if first(rec.f, "field_image_placement") == "right" else "left"
            parts.append(f'<div style="float:{side};max-width:40%;margin:0 1rem 1rem 0;">{img}</div>')
    if rec.kind == "microsites":
        parts.append(_contact(rec))
    body = first(rec.f, "body")
    if body:
        parts.append(body)
    parts += _sections(site, rec, "field_sections", out)
    att = [_media_link(site, r.get("target_id"), out) for r in refs(rec.f, "field_file_attachments")]
    att = [a for a in att if a]
    if att:
        parts.append("<h2>Documents</h2><ul>" + "".join(f"<li>{a}</li>" for a in att) + "</ul>")
    html = "\n".join(p for p in parts if p).strip()
    # links already written into the body
    for _, _, url in ATTR.findall(html):
        m = site.media_for_url(url)
        if m:
            out.media.append(m.id)
            continue
        n = site.node_for_url(url)
        if n:
            out.nodes.append(n.id)
    out.html = html
    out.media = list(dict.fromkeys(out.media))
    out.nodes = list(dict.fromkeys(x for x in out.nodes if x != rec.id))
    out.notes = list(dict.fromkeys(out.notes))
    return out


def rewrite(html: str, resolve: Callable[[str], Optional[str]]) -> tuple[str, list[tuple[str, str]]]:
    """Every href/src that resolve() gives a new address for is changed.
    Returns the new HTML and the (old, new) pairs."""
    changed = []

    def sub(m):
        old = _html.unescape(m.group(3))
        new = resolve(old) or resolve(re.sub(r"^(internal|entity)%3A", r"\1:", old, flags=re.I))
        if new and new != old:
            changed.append((old, new))
            return f"{m.group(1)}{m.group(2)}{_e(new)}{m.group(2)}"
        return m.group(0)

    return ATTR.sub(sub, html or ""), changed
