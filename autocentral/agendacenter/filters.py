"""Which agendas a step applies to.

Everything here is plain data so a plan file, the CLI menu and a future GUI
can all build the same filter. Cheap checks (category, dates, status,
description) use the agenda list only; kind checks (simple / minutes-only /
complex / empty) need each agenda's edit page, about 1 second apiece, so they
run last and only on agendas that passed everything else.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Callable, Optional

from .models import ALL_KINDS, ITEM_STATUSES, Agenda, AgendaDetails, Category, Item


def _d(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Not a date: {v!r} (use YYYY-MM-DD or M/D/YYYY)")


@dataclass
class AgendaFilter:
    categories: list[str] = field(default_factory=list)      # empty = every category
    exclude_categories: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)        # published | draft | submitted | declined; empty = any
    kinds: list[str] = field(default_factory=list)           # simple | minutes_only | complex | empty; empty = any
    meeting_from: Optional[date] = None                      # inclusive
    meeting_to: Optional[date] = None                        # inclusive
    updated_from: Optional[date] = None
    updated_to: Optional[date] = None
    description_contains: str = ""                           # case-insensitive substring
    description_regex: str = ""
    has_minutes: Optional[bool] = None
    agenda_ids: list[int] = field(default_factory=list)      # exact IDs; empty = any
    limit: Optional[int] = None                              # stop after N matches overall

    # ----------------------------------------------------------- (de)serialise
    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AgendaFilter":
        data = dict(data or {})
        for k in ("meeting_from", "meeting_to", "updated_from", "updated_to"):
            data[k] = _d(data.get(k))
        f = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        f.validate()
        return f

    def to_dict(self) -> dict:
        out = asdict(self)
        for k, v in list(out.items()):
            if isinstance(v, date):
                out[k] = v.isoformat()
        return {k: v for k, v in out.items() if v not in (None, "", [])}

    def validate(self) -> None:
        bad = [k for k in self.kinds if k not in ALL_KINDS]
        if bad:
            raise ValueError(f"Unknown agenda kind(s) {bad}; use {list(ALL_KINDS)}")
        bad = [s for s in self.statuses if s not in ("published", "draft", "submitted", "declined")]
        if bad:
            raise ValueError(f"Unknown status(es) {bad}")
        if self.description_regex:
            re.compile(self.description_regex)
        for a, b in ((self.meeting_from, self.meeting_to), (self.updated_from, self.updated_to)):
            if a and b and a > b:
                raise ValueError(f"Date range is backwards: {a} after {b}")

    # ----------------------------------------------------------------- logic
    @property
    def needs_details(self) -> bool:
        return bool(self.kinds)

    def category_selected(self, category: Category) -> bool:
        name = category.name.strip().lower()
        if self.exclude_categories and name in {c.strip().lower() for c in self.exclude_categories}:
            return False
        if not self.categories:
            return True
        return name in {c.strip().lower() for c in self.categories}

    def unknown_categories(self, existing: list[Category]) -> list[str]:
        names = {c.name.strip().lower() for c in existing}
        return [c for c in self.categories if c.strip().lower() not in names]

    def cheap_match(self, a: Agenda) -> bool:
        if self.agenda_ids and a.id not in self.agenda_ids:
            return False
        if self.statuses and a.status not in self.statuses:
            return False
        if self.has_minutes is not None and a.has_minutes != self.has_minutes:
            return False
        if not _in_range(a.meeting_date, self.meeting_from, self.meeting_to):
            return False
        if not _in_range(a.updated, self.updated_from, self.updated_to):
            return False
        if self.description_contains and self.description_contains.lower() not in a.description.lower():
            return False
        if self.description_regex and not re.search(self.description_regex, a.description, re.I):
            return False
        return True

    def detail_match(self, details: AgendaDetails) -> bool:
        return not self.kinds or details.kind in self.kinds

    def describe(self) -> str:
        parts = []
        parts.append("categories: " + (", ".join(self.categories) if self.categories else "all"))
        if self.exclude_categories:
            parts.append("except: " + ", ".join(self.exclude_categories))
        if self.statuses:
            parts.append("status: " + "/".join(self.statuses))
        if self.kinds:
            parts.append("kind: " + "/".join(self.kinds))
        if self.meeting_from or self.meeting_to:
            parts.append(f"meeting date {self.meeting_from or '…'} to {self.meeting_to or '…'}")
        if self.updated_from or self.updated_to:
            parts.append(f"updated {self.updated_from or '…'} to {self.updated_to or '…'}")
        if self.description_contains:
            parts.append(f"description contains {self.description_contains!r}")
        if self.description_regex:
            parts.append(f"description matches /{self.description_regex}/")
        if self.has_minutes is not None:
            parts.append("has minutes" if self.has_minutes else "no minutes")
        if self.agenda_ids:
            parts.append(f"{len(self.agenda_ids)} specific ID(s)")
        if self.limit:
            parts.append(f"limit {self.limit}")
        return "; ".join(parts)


ITEM_LISTS = ("items", "default_items")


@dataclass
class ItemFilter:
    """Which agenda items a step applies to. Only items that are not on an
    agenda can be acted on: the Items tab and the Default Items tab."""

    categories: list[str] = field(default_factory=list)      # empty = every category
    exclude_categories: list[str] = field(default_factory=list)
    lists: list[str] = field(default_factory=lambda: ["items"])  # items | default_items
    statuses: list[str] = field(default_factory=list)        # draft | submitted | approved | declined; empty = any
    meeting_from: Optional[date] = None                      # items only; default items have no date
    meeting_to: Optional[date] = None
    name_contains: str = ""
    name_regex: str = ""
    item_ids: list[int] = field(default_factory=list)
    limit: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ItemFilter":
        data = dict(data or {})
        for k in ("meeting_from", "meeting_to"):
            data[k] = _d(data.get(k))
        f = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        f.validate()
        return f

    def to_dict(self) -> dict:
        out = asdict(self)
        for k, v in list(out.items()):
            if isinstance(v, date):
                out[k] = v.isoformat()
        return {k: v for k, v in out.items() if v not in (None, "", [])}

    def validate(self) -> None:
        if not self.lists:
            raise ValueError("Pick at least one item list: items and/or default_items")
        bad = [x for x in self.lists if x not in ITEM_LISTS]
        if bad:
            raise ValueError(f"Unknown item list(s) {bad}; use {list(ITEM_LISTS)}")
        bad = [s for s in self.statuses if s not in ITEM_STATUSES]
        if bad:
            raise ValueError(f"Unknown item status(es) {bad}; use {list(ITEM_STATUSES)}")
        if self.name_regex:
            re.compile(self.name_regex)
        if self.meeting_from and self.meeting_to and self.meeting_from > self.meeting_to:
            raise ValueError(f"Date range is backwards: {self.meeting_from} after {self.meeting_to}")

    category_selected = AgendaFilter.category_selected
    unknown_categories = AgendaFilter.unknown_categories

    def match(self, it: Item) -> bool:
        if self.item_ids and it.id not in self.item_ids:
            return False
        if self.statuses and it.status not in self.statuses:
            return False
        if (self.meeting_from or self.meeting_to) and (
                it.default or not _in_range(it.meeting_date, self.meeting_from, self.meeting_to)):
            return False
        if self.name_contains and self.name_contains.lower() not in it.name.lower():
            return False
        if self.name_regex and not re.search(self.name_regex, it.name, re.I):
            return False
        return True

    def describe(self) -> str:
        parts = ["categories: " + (", ".join(self.categories) if self.categories else "all"),
                 " + ".join({"items": "items", "default_items": "default items"}[x] for x in self.lists)]
        if self.exclude_categories:
            parts.append("except: " + ", ".join(self.exclude_categories))
        if self.statuses:
            parts.append("status: " + "/".join(self.statuses))
        if self.meeting_from or self.meeting_to:
            parts.append(f"meeting date {self.meeting_from or '…'} to {self.meeting_to or '…'}")
        if self.name_contains:
            parts.append(f"name contains {self.name_contains!r}")
        if self.name_regex:
            parts.append(f"name matches /{self.name_regex}/")
        if self.item_ids:
            parts.append(f"{len(self.item_ids)} specific ID(s)")
        if self.limit:
            parts.append(f"limit {self.limit}")
        return "; ".join(parts)


def select_items(site, flt: ItemFilter, progress: Optional[Callable[[int, int, str], None]] = None,
                 cancel=None) -> list[Item]:
    """Resolve an item filter against the live site. Read-only."""
    categories = [c for c in site.categories(refresh=True) if flt.category_selected(c)]
    matched: list[Item] = []
    for i, cat in enumerate(categories, 1):
        if cancel:
            cancel.check()
        if progress:
            progress(i - 1, len(categories), f"Scanning items in {cat.name}")
        for lst in flt.lists:
            matched.extend(it for it in site.items(cat, default=(lst == "default_items")) if flt.match(it))
        if flt.limit and len(matched) >= flt.limit:
            matched = matched[: flt.limit]
            break
    if progress:
        progress(len(categories), len(categories), "Scan complete")
    return matched


def _in_range(v: Optional[date], lo: Optional[date], hi: Optional[date]) -> bool:
    if lo is None and hi is None:
        return True
    if v is None:
        return False
    return (lo is None or v >= lo) and (hi is None or v <= hi)


def select_agendas(site, flt: AgendaFilter, progress: Optional[Callable[[int, int, str], None]] = None,
                   cancel=None) -> list[Agenda]:
    """Resolve a filter against the live site. Read-only."""
    categories = [c for c in site.categories(refresh=True) if flt.category_selected(c)]
    matched: list[Agenda] = []
    for i, cat in enumerate(categories, 1):
        if cancel:
            cancel.check()
        if progress:
            progress(i - 1, len(categories), f"Scanning {cat.name}")
        candidates = [a for a in site.agendas(cat) if flt.cheap_match(a)]
        if flt.needs_details:
            kept = []
            for j, a in enumerate(candidates, 1):
                if cancel:
                    cancel.check()
                if progress:
                    progress(i - 1, len(categories), f"Checking type in {cat.name}: {j}/{len(candidates)}")
                if flt.detail_match(site.details(a)):
                    kept.append(a)
                    if flt.limit and len(matched) + len(kept) >= flt.limit:
                        break
            candidates = kept
        matched.extend(candidates)
        if flt.limit and len(matched) >= flt.limit:
            matched = matched[: flt.limit]
            break
    if progress:
        progress(len(categories), len(categories), "Scan complete")
    return matched
