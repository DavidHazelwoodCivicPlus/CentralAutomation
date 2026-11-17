"""Pure parsers: page HTML in, plain data out. No browser needed, so every
parser is tested offline against saved copies of real pages (tests/pages)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup, Tag

from .models import HistoryEntry


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def clean(text: Optional[str]) -> str:
    # �: the Calendar serves its date-range dash in a broken encoding
    text = (text or "").replace("\xa0", " ").replace("�", "-")
    return re.sub(r"\s+", " ", text).strip()


def module_id_of(html: str) -> Optional[int]:
    """The hidden module id every admin page carries (hdnModuleID)."""
    m = re.search(r'hdnModuleID"[^>]*?value="(\d+)"', html or "") or \
        re.search(r'value="(\d+)"[^>]*?id="[^"]*hdnModuleID"', html or "")
    return int(m.group(1)) if m else None


def page_title(html: str) -> str:
    """'Bid Postings • CP-Training19 • CivicEngage' -> 'Bid Postings'."""
    t = soup(html).title
    text = clean(t.text if t else "")
    return clean(re.split(r"\s[•|\-]\s", text)[0]) if text else ""


def site_date(text: str) -> str:
    """Site dates and date-times -> 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS'. '' if unknown."""
    text = clean(text)
    if not text:
        return ""
    for fmt, out in (("%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"), ("%m/%d/%Y %I:%M %p", "%Y-%m-%d %H:%M:00"),
                     ("%m/%d/%Y", "%Y-%m-%d"), ("%b %d, %Y", "%Y-%m-%d"), ("%B %d, %Y", "%Y-%m-%d"),
                     ("%m/%d/%y", "%Y-%m-%d")):
        try:
            return datetime.strptime(text, fmt).strftime(out)
        except ValueError:
            pass
    return text


def person(text: str) -> str:
    """History Log prints 'Hazelwood, David'; the lists print 'David Hazelwood'."""
    text = clean(text)
    if text.count(",") == 1:
        last, first = (clean(p) for p in text.split(","))
        if first and last:
            return f"{first} {last}"
    return text


def max_page(s: BeautifulSoup) -> int:
    nums = []
    for e in s.select("[data-pagenum]"):
        try:
            nums.append(int(e["data-pagenum"]))
        except (ValueError, KeyError):
            pass
    return max(nums) if nums else 1


# ===================================================================== newer
def _status_class(el: Optional[Tag]) -> str:
    if el is None:
        return ""
    cls = [c for c in (el.get("class") or []) if c != "status"]
    return (cls[0] if cls else clean(el.text)).lower()


def newer_categories(html: str) -> list[dict]:
    """Category list of Agenda Center, Form Center and Facilities:
    `.item` blocks with a checkbox and an h3 link to Category/Detail/N."""
    out = []
    for item in soup(html).select("div.item"):
        a = item.select_one("h3 a[href*='Category/Detail/']")
        if a is None:
            continue
        m = re.search(r"Category/Detail/(\d+)", a["href"])
        cb = item.select_one("input.check")
        name_attr = (cb.get("name") or "") if cb else ""
        label = _status_class(item.select_one("span.status"))
        if "Unpublished" in name_attr:
            status = label or "unpublished"
        elif "Published" in name_attr:
            status = "published"
        else:
            status = label or "published"
        counts = []
        for dt in item.select("dl dt"):
            dd = dt.find_next_sibling("dd")
            if dd is not None:
                counts.append(f"{clean(dt.text)}: {clean(dd.text)}")
        out.append({"id": int(m.group(1)), "name": clean(a.text), "status": status, "detail": "; ".join(counts)})
    return out


def agenda_list(html: str) -> dict:
    """/Admin/AgendaCenter/Agenda/AgendaList?categoryID=X&page=N"""
    s = soup(html)
    rows = []
    for r in s.select(".row.item"):
        cb = r.select_one("input.check")
        if cb is None or not cb.get("value"):
            continue
        dates = r.select("p.date")
        published = cb.get("name") == "cboxPublishedAgenda"
        label = _status_class(r.select_one("span.status"))
        status = "published" if published and label in ("", "published") else (label or "draft")
        desc = r.select_one("p.desc")
        rows.append({
            "id": int(cb["value"]),
            "name": clean(desc.text) if desc else "",
            "status": status,
            "meeting": site_date(dates[0].text) if dates else "",
            "updated": site_date(dates[1].text) if len(dates) > 1 else "",
            "minutes": r.select_one("a.minutes") is not None and
                       not re.search(r"Minutes/Edit/0\b", r.select_one("a.minutes").get("href") or ""),
        })
    return {"rows": rows, "max_page": max_page(s)}


def form_list(html: str) -> dict:
    """/Admin/FormCenter/Item/ItemList?categoryID=X&page=N (a table of forms)."""
    s = soup(html)
    rows = []
    for tr in s.select("tr.item"):
        cb = tr.select_one("input.check")
        if cb is None or not cb.get("value"):
            continue
        cell = lambda c: clean(tr.select_one(f"td.{c}").text) if tr.select_one(f"td.{c}") else ""
        name_attr = cb.get("name") or ""
        label = _status_class(tr.select_one("span.status"))
        status = "published" if "cboxPublished" in name_attr and label in ("", "published") else (label or "unpublished")
        rows.append({"id": int(cb["value"]), "name": cell("name"), "status": status,
                     "submissions": cell("submissions"), "modified": site_date(cell("lastModified"))})
    return {"rows": rows, "max_page": max_page(s)}


def facility_list(html: str) -> dict:
    """/Admin/Facilities/Category/Detail/N: one row per facility."""
    s = soup(html)
    rows = []
    for r in s.select(".row.item"):
        cb = r.select_one("input.check")
        fid = r.select_one("input[name='Facility.FacilityID']")
        value = (cb.get("value") if cb else None) or (fid.get("value") if fid else None)
        if not value:
            continue
        a = r.select_one("p.name a")
        label = _status_class(r.select_one("span.status"))
        has_children = r.select_one(f"input[name='hasChildren_{value}']")
        menu = " ".join(clean(x.text).lower() for x in r.select("ul.actionMenu li a"))
        status = label or ("published" if "unpublish" in menu else "unpublished" if "publish" in menu else "")
        reservations = r.select_one("p.date a")
        rows.append({"id": int(value), "name": clean(a.text) if a else "", "status": status,
                     "reservations": clean(reservations.text) if reservations else "",
                     "has_children": bool(has_children and has_children.get("value") == "True")})
    return {"rows": rows, "max_page": max_page(s)}


# =================================================================== classic
_SECTION = re.compile(r"^(published|unpublished|archived|draft)\b", re.I)


def _leaf_rows(s: BeautifulSoup) -> list[Tag]:
    return [tr for tr in s.select("tr") if tr.find("table") is None]


def _action_select(tr: Tag) -> Optional[Tag]:
    for sel in tr.select("select"):
        first = sel.select_one("option")
        if first is not None and "choose an action" in clean(first.text).lower():
            return sel
    return None


def _numbers(js: str) -> list[int]:
    # only plain numeric arguments, not digits inside quoted dates/strings
    args = re.sub(r"'[^']*'|\"[^\"]*\"", "", js or "")
    return [int(n) for n in re.findall(r"(?<![\w.])-?\d+(?![\w.])", args)]


def classic_rows(html: str, id_arg: int = 0) -> list[dict]:
    """Every row with a "Choose an Action" dropdown on a classic page.

    Works for category lists and item lists alike. The section (Published /
    Unpublished / Archived) comes from the heading rows above. Columns come
    from the nearest header row, lined up on its "Actions" column. The id is
    the row checkbox's value when there is one, else the `id_arg`-th number
    in the dropdown's onchange handler."""
    s = soup(html)
    out: list[dict] = []
    section = ""
    header: list[str] = []
    for tr in _leaf_rows(s):
        cells = tr.find_all(["td", "th"], recursive=False)
        texts = [clean(c.text) for c in cells]
        sel = _action_select(tr)
        if sel is None:
            joined = clean(" ".join(texts))
            if len([t for t in texts if t]) <= 2 and _SECTION.match(joined):
                section = _SECTION.match(joined).group(1).lower()
            elif "Actions" in texts and len(texts) >= 3:
                header = texts
            continue
        cb = tr.select_one("input[type=checkbox][name^=cbox]")
        nums = _numbers(sel.get("onchange") or "")
        if cb is not None and (cb.get("value") or "").isdigit():
            rid = int(cb["value"])
        elif len(nums) > id_arg:
            rid = nums[id_arg]
        else:
            continue
        # line the cells up with the header on the Actions column
        sel_idx = next((i for i, c in enumerate(cells) if sel in c.descendants), len(cells) - 1)
        cols: dict[str, str] = {}
        if "Actions" in header:
            offset = sel_idx - header.index("Actions")
            for hi, h in enumerate(header):
                ci = hi + offset
                if h and h != "Actions" and 0 <= ci < len(texts):
                    cols[h] = texts[ci]
        name = ""
        for a in tr.select("a"):
            oc = a.get("onclick") or ""
            if clean(a.text) and "OrderSingle" not in oc:
                name = clean(a.text)
                break
        if not name:
            name = next((v for h, v in cols.items() if v and h.lower() not in ("status",)), "")
        options = {clean(o.text).lower() for o in sel.select("option")}
        sec = section
        if cb is not None and not sec:
            sec = "published" if cb["name"].startswith("cboxPublished") else "unpublished"
        if not sec:
            sec = "unpublished" if "publish" in options else "published" if "unpublish" in options else ""
        out.append({"id": rid, "name": name, "section": sec, "site_status": cols.get("Status", ""),
                    "cols": cols, "options": sorted(options), "onchange": sel.get("onchange") or ""})
    return out


def classic_status(row: dict) -> str:
    sec, site = row.get("section", ""), (row.get("site_status") or "").lower()
    if sec == "archived":
        return "archived"
    if sec == "published":
        return "published"
    if site and site not in ("active", "published"):
        return site            # draft, submitted, rejected ...
    return "unpublished" if sec == "unpublished" else (site or sec)


def resdir_totals(html: str) -> dict[int, int]:
    """Resource Directory pager: '1 - 100 of 403 Listings' under each heading.
    Returns {0: unpublished total, 1: published total} (the site's changePage
    section numbers)."""
    heads = [(m.start(), 0 if m.group(1) else 1)
             for m in re.finditer(r"(Un)?[Pp]ublished Resources", html or "")]
    out: dict[int, int] = {}
    for m in re.finditer(r"of\s+(\d+)\s+Listings", html or ""):
        before = [sec for pos, sec in heads if pos < m.start()]
        if before:
            out[before[-1]] = int(m.group(1))
    return out


def calendar_list(html: str) -> list[dict]:
    """The calendars on /admin/Calendar.aspx: one checkbox per calendar,
    'County Holidays Events 56 Subscribers 0'."""
    s = soup(html)
    out = []
    for cb in s.select("input[type=checkbox][name^=cbox]"):
        value = cb.get("value") or ""
        if not value.isdigit():
            continue
        box = cb.find_parent(["tr", "li"]) or cb.parent
        link = box.select_one("a[onclick*=displayItemList]") if box else None
        text = clean(box.text) if box else ""
        events = re.search(r"Events\s+(\d+)", text)
        name = clean(link.text) if link else clean(re.split(r"\s+Events\s+\d+", text)[0])
        status = "published" if cb["name"].startswith("cboxPublished") else "unpublished"
        out.append({"id": int(value), "name": name, "status": status,
                    "events": int(events.group(1)) if events else None})
    return out


CALENDAR_VIEW_BOXES = ("ShowMineOnly", "Published", "Draft", "Pending", "Submited", "Rejected", "Previous")


def calendar_view_options(html: str) -> dict[str, bool]:
    """The Calendar's saved Display Events boxes (Only mine, Published,
    Draft, Pending, Submitted, Declined, Previous)."""
    s = soup(html)
    out = {}
    for name in CALENDAR_VIEW_BOXES:
        el = s.find("input", id=name)
        if el is not None:
            out[name] = el.has_attr("checked")
    return out


def calendar_events(html: str) -> dict:
    """One page of a calendar's event list (admin, list view).

    The page also carries every event id of the calendar in hidden fields
    (hdnallPublished / hdnallUnpublished) and the total (hdnallAllEventCount),
    so the caller can check it read them all."""
    s = soup(html)
    rows = []
    for it in s.select("div.item[data-event]"):
        cb = it.select_one("input.check")
        a = it.select_one("h3 a")
        st = it.select_one("span.status")
        when = it.select_one("p")
        label = _status_class(st)
        published = bool(cb and cb.get("name") == "cboxPublished")
        if published:
            status = "published"
        else:
            status = label if label and label not in ("previous", "published") else "unpublished"
        rows.append({"id": int(it["data-event"]), "name": clean(a.text) if a else "", "status": status,
                     "when": clean(when.text) if when else "", "past": label == "previous"})
    ids: set[int] = set()
    for hid in ("hdnallPublished", "hdnallUnpublished"):
        el = s.find("input", id=hid)
        if el is not None:
            ids |= {int(x) for x in (el.get("value") or "").split(",") if x.strip().isdigit()}
    total_el = s.find("input", id="hdnallAllEventCount")
    total = int(total_el["value"]) if total_el is not None and (total_el.get("value") or "").isdigit() else None
    pager = re.findall(r"__doPostBack\('([^']*Pager[^']*)','(\d+)'\)", html or "")
    return {"rows": rows, "all_ids": ids, "total": total,
            "pager": pager[0][0] if pager else "", "max_page": max([int(p) for _, p in pager], default=1)}


def staff_tree(html: str) -> list[dict]:
    """Staff Directory department tree (after Expand Current View).

    Each department is a div#divN with cnstatus (40 = published, 10 =
    unpublished) holding #categoryN (an <a> when it has children, a <span>
    when it's a leaf). Nesting of the divs is the department path."""
    s = soup(html)
    out = []
    for div in s.find_all("div", id=re.compile(r"^div\d+$")):
        nid = int(div["id"][3:])
        if nid == 0:
            continue                      # the "Staff Directory" root
        label = div.find(id=f"category{nid}")
        if label is None:
            continue
        unpublished = label.select_one(".unpub") is not None or div.get("cnstatus") == "10"
        name = clean(re.sub(r"\(\s*Unpublished\s*\)", "", label.get_text(" ")))
        parent = div.find_parent("div", id=re.compile(r"^div\d+$"))
        pid = int(parent["id"][3:]) if parent is not None else 0
        out.append({"id": nid, "name": name, "status": "unpublished" if unpublished else "published",
                    "parent": pid or None, "employees": int(label.get("cn_i") or 0)})
    by_id = {d["id"]: d for d in out}
    for d in out:
        path, p = [], d["parent"]
        while p and p in by_id and len(path) < 20:
            path.insert(0, by_id[p]["name"])
            p = by_id[p]["parent"]
        d["path"] = path
    return out


def staff_export(data: bytes) -> dict:
    """The Staff Directory's own Export Staff workbook: a Departments sheet
    (Name, ..., Hidden, Sub-Department1..N = its parents) and, when the site
    has staff, a sheet of employees. Returns {"departments": [...], "employees": [...]}."""
    import io
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out: dict[str, list[dict]] = {"departments": [], "employees": []}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        head = [clean(str(h or "")) for h in rows[0]]
        kind = "departments" if ws.title.lower().startswith("depart") else "employees"
        for r in rows[1:]:
            rec = {h: ("" if v is None else str(v).strip()) for h, v in zip(head, r) if h}
            if any(rec.values()):
                rec["_sheet"] = ws.title
                out[kind].append(rec)
    return out


# =============================================================== history log
def history_page(html: str) -> dict:
    """One page of History Log search results."""
    s = soup(html)
    entries: list[HistoryEntry] = []
    module = ""
    table = None
    for t in s.select("table"):
        first = t.select_one("tr")
        if first is not None and "Title/Item" in clean(first.text) and t.find("table") is None:
            table = t
            break
    if table is not None:
        for tr in table.select("tr")[1:]:
            tds = [clean(td.text) for td in tr.find_all("td", recursive=False)]
            if len(tds) == 1 and tds[0]:
                module = tds[0]
            elif len(tds) >= 5 and tds[1]:
                entries.append(HistoryEntry(title=tds[0], when=site_date(tds[1]), who=person(tds[2]),
                                            ids=tds[3], action=tds[4], module=module))
    pages = [int(n) for n in re.findall(r"pageNumber\((\d+)\)", html or "")]
    return {"entries": entries, "max_page": max(pages) if pages else 1, "found_table": table is not None}
