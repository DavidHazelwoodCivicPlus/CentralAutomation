"""The last read of each module, saved per site, so the window shows it again
when it's reopened: cache/<host>/inventory_<module>.json."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .models import InventoryRow, ModuleScan


def _path(folder: Path, key: str) -> Path:
    return folder / f"inventory_{key}.json"


def save_scan(folder: Path, ms: ModuleScan) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    data = asdict(ms)
    _path(folder, ms.module).write_text(json.dumps(data), encoding="utf-8")


def load_scan(folder: Path, key: str) -> Optional[ModuleScan]:
    p = _path(folder, key)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = [InventoryRow(**r) for r in data.pop("rows")]
        return ModuleScan(rows=rows, **data)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def load_all(folder: Path, keys: list[str]) -> dict[str, ModuleScan]:
    out = {}
    for k in keys:
        ms = load_scan(folder, k)
        if ms is not None:
            out[k] = ms
    return out
