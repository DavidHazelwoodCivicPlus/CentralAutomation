"""Data shared by every module: one inventory row per category or item."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# Columns in the order the table and the spreadsheet show them.
COLUMNS = [
    ("module", "Module"),
    ("level", "Level"),
    ("category", "Category"),
    ("name", "Name"),
    ("status", "Status"),
    ("created_on", "Created"),
    ("created_by", "Created By"),
    ("modified_on", "Last Modified"),
    ("modified_by", "Modified By"),
    ("detail", "Detail"),
    ("ids", "IDs"),
    ("category_id", "Category ID"),
    ("item_id", "Item ID"),
]


@dataclass
class InventoryRow:
    module: str                    # module key, e.g. "bids"
    level: str                     # "category" or "item"
    category: str                  # the category's name (for categories: their own name)
    name: str
    status: str = ""               # published / unpublished / draft / archived ...
    category_id: Optional[int] = None
    item_id: Optional[int] = None  # None for category rows
    created_on: str = ""           # "YYYY-MM-DD HH:MM:SS" from History Log, or "" if unknown
    created_by: str = ""
    modified_on: str = ""          # from the list or History Log, when the site shows it
    modified_by: str = ""
    detail: str = ""               # module-specific extras: "Meeting 2024-03-05", "12 submissions" ...
    ids: str = ""                  # the site's own IDs as History Log prints them, once matched
    module_label: str = ""         # the name this site gives the module
    fields: dict = field(default_factory=dict)   # module's own columns: {"Meeting date": "2024-03-05", ...}
    extra: dict = field(default_factory=dict)    # internal: history_starts, parent_id ...

    @property
    def own_id(self) -> Optional[int]:
        return self.item_id if self.level == "item" else self.category_id

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra")
        d.pop("fields")
        d["module"] = self.module_label or self.module
        return d


@dataclass
class HistoryEntry:
    """One History Log row."""

    title: str
    when: str          # "YYYY-MM-DD HH:MM:SS"
    who: str           # "First Last"
    ids: str           # as printed: "AMID 38 ADID 4400"
    action: str        # Create / Modify / Delete ...
    module: str = ""   # module heading the row was under

    @property
    def id_pairs(self) -> list[tuple[str, int]]:
        import re
        return [(k, int(v)) for k, v in re.findall(r"([A-Za-z]+)\s*(-?\d+)", self.ids)]


@dataclass
class ModuleScan:
    """What scanning one module produced."""

    module: str
    label: str = ""
    present: bool = True
    rows: list[InventoryRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # pages that failed, things not read
    seconds: float = 0.0
    read_at: str = ""          # "YYYY-MM-DD HH:MM" when this module was read
