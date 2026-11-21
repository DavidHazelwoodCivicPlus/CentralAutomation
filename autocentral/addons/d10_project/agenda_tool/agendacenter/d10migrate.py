"""A whole D10 Agenda Center migration, from the old site's JSON exports.

Getting the import ready (no Central site needed):
  1. build   run the D10-to-Central Migration Tool hidden (d10engine): the
             import workbook, the list of files, the post-import tasks,
             categories, flagged list, No-Agenda template
  2. files   download every file from the old site (d10files), into
             <site>-agenda-center-files (the import) and
             <site>-agenda-center-attach-after-import (added after import)
  3. batches zips under 1 GB from the workbook and the downloaded files
             (batchzip), rows whose files couldn't be downloaded left out

Everything lands in one work folder (by default "central-import" inside the
exports folder), with migration.json saying what's been done. Running again
re-uses what's there: files already downloaded aren't fetched again.

The site part (categories, Content Import, publish, the to-do list) is
AgendaJobs.migrate, run from the window as a dry run first.
"""

from __future__ import annotations

import json
import shutil
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .batchzip import DEFAULT_LIMIT_MB, FolderSource, batch_zips, plan_batches, write_batches
from .d10engine import ATTACH, IN_ZIP, export_files, run_tool
from .d10files import download_all

STATE = "migration.json"


@dataclass
class MigrationState:
    work: str = ""
    site: str = ""                      # the migration tool's name for the site, e.g. yorksc
    tool: str = ""
    exports: str = ""
    zone: str = ""
    workbook: str = ""
    tasks_json: str = ""
    categories: list[str] = field(default_factory=list)
    files_dir: str = ""
    attach_dir: str = ""
    batches_dir: str = ""
    built_at: str = ""
    downloaded_at: str = ""
    batched_at: str = ""
    file_counts: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    seconds: dict = field(default_factory=dict)        # step -> how long it took (build, download, batches)

    @classmethod
    def load(cls, work: Path) -> Optional["MigrationState"]:
        p = Path(work) / STATE
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        Path(self.work).mkdir(parents=True, exist_ok=True)
        (Path(self.work) / STATE).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def zips(self) -> list[Path]:
        return batch_zips(Path(self.batches_dir)) if self.batches_dir else []

    def describe(self) -> str:
        if not self.built_at:
            return "Nothing done yet."
        lines = [f"Site: {self.site}  (old site's time zone: {self.zone})",
                 f"1. Import built {self.built_at}: {len(self.categories)} categories."]
        if self.downloaded_at:
            c = self.file_counts
            lines.append(f"2. Files downloaded {self.downloaded_at}: {c.get('ok', 0)} ok"
                         + "".join(f", {v} {k.lower()}" for k, v in c.items() if k != "ok"))
        if self.batched_at:
            lines.append(f"3. Batches made {self.batched_at}: {len(self.zips())} zip(s) in {self.batches_dir}")
        return "\n".join(lines + self.notes)


def default_work(exports: Path) -> Path:
    return Path(exports) / "central-import"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def build(tool: Path, exports: Path, zone: str, work: Path, log: Callable[[str], None]) -> MigrationState:
    """Step 1: the migration tool's outputs, in the work folder."""
    work = Path(work)
    t0 = time.monotonic()
    engine_dir = work / "migration-tool-output"
    if engine_dir.exists():
        shutil.rmtree(engine_dir)
    res = run_tool(Path(tool), export_files(Path(exports)), zone, engine_dir, log=log)
    log(res.summary)
    old = MigrationState.load(work)
    st = MigrationState(work=str(work), site=res.site, tool=str(tool), exports=str(exports), zone=zone,
                        workbook=str(res.workbook), tasks_json=str(res.tasks_json or ""),
                        categories=res.categories,
                        files_dir=str(work / f"{res.site}-agenda-center-files"),
                        attach_dir=str(work / f"{res.site}-agenda-center-attach-after-import"),
                        batches_dir=str(work / f"{res.site}-import-batches"), built_at=_now(),
                        seconds={"build": round(time.monotonic() - t0, 1)})
    if old and old.site and old.site != res.site:
        raise ValueError(f"This work folder belongs to {old.site}, not {res.site}. Pick another work folder.")
    (engine_dir / "files.json").write_text(json.dumps(
        {"to_get": [asdict(f) for f in res.to_get], "unpublished": res.unpublished}, indent=2), encoding="utf-8")
    st.save()
    return st


def download(st: MigrationState, log: Callable[[str], None], cancelled: Callable[[], bool] = lambda: False,
             workers: int = 6) -> MigrationState:
    """Step 2: every file, from the old site. Files already there are kept."""
    from .d10engine import FileToGet
    t0 = time.monotonic()
    engine_dir = Path(st.workbook).parent
    data = json.loads((engine_dir / "files.json").read_text(encoding="utf-8"))
    files = [FileToGet(**f) for f in data["to_get"]]
    files_dir, attach_dir = Path(st.files_dir), Path(st.attach_dir)
    files_dir.mkdir(parents=True, exist_ok=True)
    template = engine_dir / "No-Agenda-Template.pdf"
    if template.is_file():
        shutil.copy2(template, files_dir / template.name)
    n_zip = sum(1 for f in files if f.dest == IN_ZIP)
    log(f"Downloading {len(files)} file(s) from the old site ({n_zip} for the import, "
        f"{len(files) - n_zip} to attach after import). Files already downloaded are skipped.")
    outcomes = download_all(files, files_dir, attach_dir, Path(st.work) / f"{st.site}-agenda-center-file-log.csv",
                            data.get("unpublished"), workers=workers, log=log, cancelled=cancelled)
    c = Counter("ok" if o.result in ("ok", "skipped") else o.result for o in outcomes)
    if data.get("unpublished"):
        c["Omitted - unpublished"] = len(data["unpublished"])
    st.file_counts = dict(c)
    st.downloaded_at = _now()
    st.seconds["download"] = round(time.monotonic() - t0, 1)
    st.save()
    bad = sum(v for k, v in c.items() if k != "ok")
    log(f"Files: {c['ok']} of {len(files)} downloaded."
        + (f" {bad} couldn't be (see {st.site}-agenda-center-file-log.csv); rows that need them are left out "
           "of the batches." if bad else ""))
    return st


def make_batches(st: MigrationState, limit_mb: int, log: Callable[[str], None],
                 cancelled: Callable[[], bool] = lambda: False) -> MigrationState:
    """Step 3: zips under 1 GB from the workbook and the downloaded files."""
    t0 = time.monotonic()
    source = FolderSource(Path(st.workbook), [Path(st.files_dir)])
    plan = plan_batches(source, limit_mb=limit_mb)
    log(plan.summary())
    write_batches(plan, Path(st.batches_dir), log=log, cancelled=cancelled)
    up = Path(st.batches_dir) / "uploaded.json"
    if up.is_file():
        up.unlink()
    st.batched_at = _now()
    st.seconds["batches"] = round(time.monotonic() - t0, 1)
    st.save()
    return st


def prepare(tool: Path, exports: Path, zone: str, work: Path, limit_mb: int = DEFAULT_LIMIT_MB,
            log: Callable[[str], None] = print, cancelled: Callable[[], bool] = lambda: False,
            rebuild: bool = True) -> MigrationState:
    """Steps 1-3 in a row."""
    st = MigrationState.load(Path(work))
    if rebuild or st is None or not st.built_at:
        st = build(tool, exports, zone, work, log)
    if cancelled():
        raise InterruptedError("Stopped.")
    st = download(st, log, cancelled)
    if cancelled():
        raise InterruptedError("Stopped.")
    return make_batches(st, limit_mb, log, cancelled)


def start_times(exports: Path, top: int = 2) -> dict[str, list[tuple[str, int]]]:
    """The most common meeting start times in each time zone, so the old
    site's zone can be picked by eye (meetings rarely start at 3 am)."""
    from .d10 import ZONES as D10_ZONES, load_meetings_json
    hits = sorted(Path(exports).glob("*meetings.json"))
    if not hits:
        return {}
    out = {}
    for zone in D10_ZONES:
        c = Counter(m.when.strftime("%I:%M %p").lstrip("0") for m in load_meetings_json(hits[0], zone) if m.when)
        out[zone] = c.most_common(top)
    return out
