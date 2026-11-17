"""Plain data types shared across the tool. No Selenium in here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

# Agenda "kinds" as the tool understands them.
SIMPLE = "simple"              # agenda file, with or without a minutes file
MINUTES_ONLY = "minutes_only"  # no agenda file, has a minutes file
COMPLEX = "complex"            # built from items in the agenda builder
EMPTY = "empty"                # no agenda file and no minutes (a bare shell)
ALL_KINDS = (SIMPLE, MINUTES_ONLY, COMPLEX, EMPTY)


def parse_site_date(text: str) -> Optional[date]:
    """Site dates look like 6/12/2024 (sometimes with a time after them)."""
    text = (text or "").strip().split(" ")[0]
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def site_date(d: date) -> str:
    """Format a date the way the site's date fields accept it: M/D/YYYY."""
    return f"{d.month}/{d.day}/{d.year}"


@dataclass
class Category:
    id: int
    name: str
    published: bool


ITEM_STATUSES = ("draft", "submitted", "approved", "declined")


@dataclass
class Item:
    """An agenda item that is not on an agenda (the category's Items tab), or a
    default item (Default Items tab). Items placed on an agenda belong to that
    agenda and are deleted with it."""

    id: int
    category_id: int
    category_name: str
    name: str
    status: str               # draft | submitted | approved | declined
    default: bool             # True = Default Items tab
    meeting_date: Optional[date] = None
    folder_id: int = 0        # 0 = not in a folder


@dataclass
class Folder:
    id: int
    category_id: int
    name: str
    item_count: int


@dataclass
class Workflow:
    id: int
    category_id: int
    name: str


@dataclass
class Agenda:
    """One row from a category's agenda list (cheap to get: no extra page loads)."""

    id: int
    category_id: int
    category_name: str
    meeting_date: Optional[date]
    updated: Optional[date]
    description: str
    published: bool
    status: str               # published | draft | submitted | declined
    has_minutes: bool
    minutes_id: int = 0
    # Filled in only when details are fetched (about 1 second per agenda).
    details: Optional["AgendaDetails"] = None


@dataclass
class AgendaDetails:
    is_simple: bool
    agenda_file: str
    minutes_file: str
    minutes_id: int
    item_count: int = 0
    item_files: list[str] = field(default_factory=list)
    minutes_status: str = ""

    @property
    def kind(self) -> str:
        if not self.is_simple and self.item_count > 0:
            return COMPLEX
        if self.agenda_file and self.is_simple:
            return SIMPLE
        if self.minutes_file or self.minutes_id:
            return MINUTES_ONLY
        # Bare agenda: no file, no items, no minutes. (Non-simple agendas get
        # an auto-generated agenda PDF name such as 02112020.pdf; that is
        # not an uploaded file.)
        return EMPTY


@dataclass
class ImportItem:
    name: str
    files: list[str]
    meeting_time: str = ""
    description: str = ""


@dataclass
class Meeting:
    """One agenda to create, merged from the workbook and the JSON."""

    category: str
    meeting_date: date
    description: str            # final description (custom name already applied)
    source_description: str     # what the source file said, may be blank
    agenda_file: str = ""
    minutes_file: str = ""
    items: list[ImportItem] = field(default_factory=list)
    attach_minutes_file: str = ""   # JSON "attach-minutes": added with Add Minutes after publishing
    video_link: str = ""
    additional_video_link: str = ""
    source: str = "workbook"    # workbook | json
    source_row: int = 0
    json_tasks: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    file_urls: dict = field(default_factory=dict)   # file name -> where the old (D10) site served it
    notes: list[str] = field(default_factory=list)   # how dates / files were worked out

    @property
    def kind(self) -> str:
        if self.items:
            return COMPLEX
        if self.agenda_file:
            return SIMPLE
        if self.minutes_file:
            return MINUTES_ONLY
        return EMPTY

    def all_files(self) -> list[str]:
        files = [f for f in (self.agenda_file, self.minutes_file) if f]
        for it in self.items:
            files.extend(it.files)
        if self.attach_minutes_file:
            files.append(self.attach_minutes_file)
        return files

    @property
    def later_minutes(self) -> str:
        """Minutes file added through Add Minutes once the agenda is published."""
        if self.kind == MINUTES_ONLY:
            return self.minutes_file
        return self.attach_minutes_file
