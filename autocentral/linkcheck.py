"""Does each link on the pages work?

Site links are checked from inside the logged-in page (relay.js
links.check), so unpublished pages and admin-only files answer as they do for
you. Outside sites are checked by the tool itself (the browser won't let a
page read another site's answer), several at a time, each address once.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from urllib.parse import urlparse

from .models import InventoryRow
from .sitecontent import unique_urls

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) link check"


def check_external(url: str, timeout: float = 12.0) -> tuple[bool, str]:
    """(works, why not). HEAD first, GET when a server refuses HEAD."""
    ctx = ssl.create_default_context()
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": _UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return (200 <= r.status < 400), ("" if r.status < 400 else f"HTTP {r.status}")
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (400, 403, 404, 405, 429, 500, 501, 503):
                continue                    # some servers only answer GET properly
            why = f"HTTP {e.code}" + (" (the site may block automated checks)" if e.code in (401, 403, 429) else "")
            return False, why
        except Exception as e:              # DNS, refused, timeout, bad certificate ...
            if method == "HEAD":
                continue
            return False, f"no answer ({type(e).__name__}: {str(e)[:80]})"
    return False, "no answer"


def check_rows(rows: list[InventoryRow], site_host: str, relay, log: Callable[[str], None],
               cancel_check: Optional[Callable[[], None]] = None, workers: int = 16,
               progress: Optional[Callable[[int, int, str], None]] = None) -> dict[str, tuple[bool, str]]:
    """Check every distinct link / file / image address on these rows. Writes
    "Works" on each item row and "Broken links" on each page row. Returns
    {url: (works, why)}."""
    urls = unique_urls(rows)
    internal = [u for u in urls if not urlparse(u).netloc or urlparse(u).netloc.lower() == site_host.lower()]
    external = [u for u in urls if u not in set(internal) and urlparse(u).scheme in ("http", "https")]
    results: dict[str, tuple[bool, str]] = {}
    log(f"Checking {len(internal)} site address(es) and {len(external)} outside address(es)...")
    for i in range(0, len(internal), 150):
        if cancel_check:
            cancel_check()
        got = relay.call("links", "check", internal[i:i + 150]) or {}
        for u, r in got.items():
            results[u] = (bool(r.get("ok")), r.get("why") or "")
        log(f"Site addresses: {min(i + 150, len(internal))} of {len(internal)} checked.")
        if progress:
            progress(len(results), len(urls), "links")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {u: pool.submit(check_external, u) for u in external}
        for n, (u, f) in enumerate(futures.items(), 1):
            if cancel_check:
                cancel_check()
            results[u] = f.result()
            if progress:
                progress(len(results), len(urls), "links")
            if n % 100 == 0 or n == len(futures):
                log(f"Outside addresses: {n} of {len(futures)} checked.")
    broken_on: dict[int, int] = {}
    for r in rows:
        if r.level != "item":
            continue
        u = r.fields.get("URL", "")
        if u in results:
            ok, why = results[u]
            r.fields["Works"] = "yes" if ok else f"no: {why}"
            if not ok:
                broken_on[r.category_id] = broken_on.get(r.category_id, 0) + 1
    for r in rows:
        if r.level == "category":
            r.fields["Broken links"] = str(broken_on.get(r.category_id, 0)) if broken_on.get(r.category_id) else ""
    bad = sum(1 for ok, _ in results.values() if not ok)
    log(f"Links checked: {len(results)} address(es), {bad} not working.")
    return results
