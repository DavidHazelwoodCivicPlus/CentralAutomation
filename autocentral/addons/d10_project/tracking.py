"""The tracking workbook: where everything from the D10 site went and what's
left. One sheet each: Steps, Pages, Files, Links, Modules, To do."""

from __future__ import annotations

from pathlib import Path


def _w(ws, widths):
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _sheet(wb, title, header, rows, widths):
    from openpyxl.styles import Font, PatternFill
    ws = wb.create_sheet(title)
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
    for r in rows:
        ws.append([("" if v is None else (", ".join(map(str, v)) if isinstance(v, list) else v)) for v in r])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    _w(ws, widths)
    return ws


def write_tracking(pr, path: Path) -> Path:
    import openpyxl
    from .runner import STEPS
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    host = f"https://{pr.central}" if pr.central else ""
    _sheet(wb, "Steps", ["Step", "State", "When", "Note"],
           [[label, pr.steps.get(k, {}).get("state", ""), pr.steps.get(k, {}).get("at", ""), pr.steps.get(k, {}).get("note", "")]
            for k, label in STEPS], [44, 12, 20, 100])
    by = {p["key"]: p for p in pr.pages}

    def trail(p):
        out, seen = [], set()
        while p and p["key"] not in seen:
            seen.add(p["key"])
            out.append(p["title"])
            p = by.get(p["parent"])
        return " > ".join(reversed(out))
    _sheet(wb, "Pages", ["Page", "Where", "Kind", "State", "Old address", "New address", "Central ID", "Links to pages",
                         "Files", "Notes", "Detail"],
           [[p["title"], trail(p), p["kind"], p["state"], p["old_url"], (host + p["new_url"]) if p.get("new_url") else "",
             p.get("new_id"), len(p["nodes"]), len(p["media"]), "; ".join(p["notes"] + ([f"links to {p['link_to']}"] if p.get("link_to") else [])),
             p.get("detail", "")] for p in pr.pages], [34, 60, 9, 11, 60, 50, 10, 8, 7, 60, 40])
    _sheet(wb, "Files", ["File", "Title", "Kind", "Top folder", "Folder", "Downloaded", "MB", "In Central", "New address",
                         "Old address", "Used on"],
           [[f["file"], f["title"], "picture" if f["image"] else "document", f["root"], f["folder"], f["download"],
             round((f.get("size") or 0) / 2**20, 2), f["imported"], (host + f["new_url"]) if f.get("new_url") else "",
             f["url"], f["used_on"]] for f in pr.files], [36, 36, 9, 22, 28, 12, 7, 9, 50, 70, 50])
    _sheet(wb, "Links", ["Where", "Old address", "New address", "Kind", "State"],
           [[ln["where"], ln["old"], ln["new"], ln["kind"], ln["state"]] for ln in pr.links], [40, 70, 50, 10, 12])
    mods = []
    for kind, label, name in (("news", "News Flash", "title"), ("jobs", "Jobs", "title"), ("archive", "Archive Center", "title"),
                              ("events", "Calendar", "title"), ("staff", "Staff Directory", "last"),
                              ("business", "Resource Directory", "name")):
        for r in getattr(pr, kind):
            nm = f"{r['last']}, {r['first']}" if kind == "staff" else r.get(name, "")
            mods.append([label, nm, r.get("category") or r.get("department") or r.get("calendar") or "", r["state"],
                         r.get("old_url", ""), r.get("detail", "")])
    _sheet(wb, "Modules", ["Module", "Item", "Category", "State", "Old address", "Detail"], mods, [18, 44, 26, 12, 60, 40])
    _sheet(wb, "Check", ["Module", "Item", "Issue", "Where / detail"],
           [[c["module"], c["item"], c["issue"], c["detail"]] for c in (getattr(pr, "check", None) or [])], [20, 50, 12, 60])
    _sheet(wb, "To do", ["What", "Where", "Detail", "State"],
           [[t["what"], t["where"], t["detail"], t["state"]] for t in pr.tasks], [22, 40, 80, 14])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
