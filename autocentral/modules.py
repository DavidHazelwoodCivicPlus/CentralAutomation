"""The modules the tool knows, found by their fixed admin URL.

Menu names differ between sites ("Business Directory" vs "Resource
Directory"), but the end of each module's admin URL is the same on every
Central site that has the module. The tool goes straight to these URLs and
checks the page's hidden module id (hdnModuleID) to confirm it landed on the
module, so a module missing from a site shows up as "not on this site".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleDef:
    key: str
    label: str            # our name; the site's own name is read from its page
    path: str             # admin URL path
    style: str            # newer | classic | calendar | resdir | staff
    module_id: int        # hdnModuleID on its pages, and the History Log module filter


MODULES: list[ModuleDef] = [
    ModuleDef("agenda", "Agenda Center", "/Admin/AgendaCenter", "newer", 65),
    ModuleDef("archive", "Archive Center", "/admin/Archive.aspx", "classic", 15),
    ModuleDef("bids", "Bids", "/admin/Bids.aspx", "classic", 16),
    ModuleDef("calendar", "Calendar", "/admin/Calendar.aspx", "calendar", 58),
    ModuleDef("facilities", "Facilities", "/Admin/Facilities", "newer", 79),
    ModuleDef("faq", "FAQs", "/admin/FAQ.aspx", "classic", 25),
    ModuleDef("forms", "Form Center", "/Admin/FormCenter", "newer", 71),
    ModuleDef("resdir", "Resource Directory", "/admin/BusinessDirectoryii.aspx", "resdir", 12),
    ModuleDef("news", "News Flash", "/admin/CivicAlerts.aspx", "classic", 1),
    ModuleDef("staff", "Staff Directory", "/admin/Directory.aspx", "staff", 4),
]

BY_KEY = {m.key: m for m in MODULES}


def get(key: str) -> ModuleDef:
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(f"Unknown module {key!r}. Known: {', '.join(BY_KEY)}") from None
