"""Command line: scan a site and write the inventory spreadsheet.

    scan.bat                                   asks for the site
    scan.bat --site https://cp-training19.civicplus.com --modules bids,faq
    scan.bat --attach 127.0.0.1:9222 ...       use a Chrome started with --remote-debugging-port
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .browser import Browser
from .inventory import export_xlsx
from .modules import BY_KEY, MODULES, SLOW_KEYS
from .reporting import Cancelled, Reporter
from .scanner import Scanner

ROOT = Path(__file__).resolve().parent.parent
INVENTORIES = ROOT / "inventories"


def cache_dir(host: str) -> Path:
    """Saved History Log per site, so later scans only read what's new."""
    return ROOT / "cache" / host.split(":")[0]


def default_out(site: str) -> Path:
    host = urlparse(site if "://" in site else "https://" + site).netloc.split(".")[0] or "site"
    return INVENTORIES / f"{host}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="autocentral", description="Read-only inventory of a Central site.")
    ap.add_argument("--scan", action="store_true", help="scan (default when run from scan.bat)")
    ap.add_argument("--site", help="site URL, e.g. https://cp-training19.civicplus.com")
    ap.add_argument("--attach", help="debugger address of an already-open Chrome, e.g. 127.0.0.1:9222")
    ap.add_argument("--modules", help="comma-separated: " + ",".join(BY_KEY))
    ap.add_argument("--no-history", action="store_true", help="skip History Log (no created by / date)")
    ap.add_argument("--history-refresh", action="store_true",
                    help="read the whole History Log again instead of only what's new since the last scan")
    ap.add_argument("--parallel", type=int, default=4, help="pages read at once (default 4)")
    ap.add_argument("--out", help="spreadsheet path (default inventories\\<site>_<time>.xlsx)")
    a = ap.parse_args(argv)

    site = a.site or input("Site URL (e.g. https://cp-training19.civicplus.com): ").strip()
    if not site:
        return 1
    keys = [k.strip() for k in (a.modules or "").split(",") if k.strip()]
    for k in keys:
        if k not in BY_KEY:
            print(f"Unknown module {k!r}. Known: {', '.join(BY_KEY)}")
            return 2
    # Pages and Documents take minutes on a big site: only read when asked for (--modules pages,docs).
    keys = keys or [m.key for m in MODULES if m.key not in SLOW_KEYS]

    def log(level: str, msg: str) -> None:
        print(("! " if level in ("warn", "error") else "  ") + msg, flush=True)

    rep = Reporter(on_log=log)
    b = Browser(site, rep)
    b.start(a.attach)
    try:
        if not b.is_logged_in():
            b.open_for_login()
            print("Log in in the Chrome window (including 2FA). The scan starts by itself once you're in.")
            b.wait_for_login()
        print("Logged in. Scanning (read-only)...")
        scanner = Scanner(b, parallel=a.parallel, cache_dir=cache_dir(b.host),
                          refresh_history=a.history_refresh)
        scans = scanner.scan(keys or None, with_history=not a.no_history)
        out = export_xlsx(Path(a.out) if a.out else default_out(site), b.site_url, scans)
        print()
        print(f"{'Module':32} {'Categories':>10} {'Items':>7} {'Creator known':>14} {'Time':>6}")
        for s in scans:
            if not s.present:
                print(f"{s.label:32} {'not on this site':>40}")
                continue
            cats = sum(1 for r in s.rows if r.level == "category")
            known = sum(1 for r in s.rows if r.created_by)
            print(f"{s.label[:32]:32} {cats:>10} {len(s.rows) - cats:>7} {known:>7}/{len(s.rows):<6} {s.seconds:>5.0f}s")
        print(f"\nSaved: {out}")
        return 0
    except Cancelled:
        print("Stopped.")
        return 1
    finally:
        if not a.attach:
            b.quit()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
