"""D10 project add-on: migrate one D10 site into Central from its JSON
exports (pages, files, pictures, news, jobs, staff, archive, calendar,
Resource Directory and Agenda Center), tracking every piece.
Switch it off with "addons": {"d10_project": false} in settings.json, or
delete this folder (and its line in autocentral/addons/__init__.py)."""

LABEL = "D10 project"


def add_tab(app, nb):
    from .gui_tab import ProjectTab
    tab = ProjectTab(app, nb)
    nb.add(tab.frame, text=LABEL)
    return tab
