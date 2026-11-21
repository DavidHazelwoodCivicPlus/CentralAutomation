"""Add-ons: extra tabs for one-off projects, switched on or off in
settings.json ("addons": {"d10_project": true}). Each add-on is a package
here with add_tab(app, notebook). To remove one for good, delete its folder
and its line in ADDONS; nothing else in the tool uses it.
"""

from __future__ import annotations

import importlib

ADDONS = {  # key -> (package, on by default)
    "d10_project": ("autocentral.addons.d10_project", True),
}


def enabled(settings: dict) -> list[str]:
    chosen = settings.get("addons") or {}
    return [k for k, (_, default) in ADDONS.items() if chosen.get(k, default)]


def add_tabs(app, nb) -> dict:
    """Adds each switched-on add-on's tab; one that fails to load is skipped
    (and said in the log) so the rest of the tool still opens."""
    out = {}
    for key in enabled(getattr(app, "settings", {}) or {}):
        try:
            mod = importlib.import_module(ADDONS[key][0])
            out[key] = mod.add_tab(app, nb)
        except Exception as e:
            try:
                app.log(f"Add-on {key} didn't load: {e}", "warn")
            except Exception:
                pass
    return out
