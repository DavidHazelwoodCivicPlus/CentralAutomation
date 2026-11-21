"""Runs relay.js (the in-page action library) in the logged-in Chrome tab.

Each call loads the library into the page (once per page: it checks its own
version), starts the call without waiting, then checks on it every few
hundredths of a second. So a long call never meets Selenium's 120 s command
limit, Stop works between calls, and a quick call costs almost nothing extra.

Calls are made one at a time: Central works through requests one by one, and
several at once only made it slower (measured on training19, 2026-11-19).
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from ..browser import Browser, SiteError
from ..reporting import Cancelled

_SRC = (Path(__file__).with_name("relay.js")).read_text(encoding="utf-8")
LIB = _SRC.replace("__VERSION__", hashlib.sha1(_SRC.encode("utf-8")).hexdigest()[:12])

# relay.js module names for our module keys (Agenda Center has its own code).
RELAY_KEY = {"archive": "archive", "bids": "bids", "calendar": "calendar", "facilities": "facilities",
             "faq": "faq", "forms": "forms", "resdir": "resdir", "news": "news", "staff": "staff",
             "pages": "pages", "docs": "docs"}

CHUNK = 150          # IDs per call: what the bulk forms took in one request on training19

_START = LIB + r"""
;const [mod, op, args] = arguments;
const t = 'rl' + Date.now() + Math.random().toString(36).slice(2);
window.__acCalls = window.__acCalls || {};
const s = window.__acCalls[t] = {done: false};
window.__acRelay.call(mod, op, args).then(v => { s.value = v; s.done = true; },
                                          e => { s.error = String(e && e.message || e); s.done = true; });
return t;
"""

_POLL = r"""
const s = (window.__acCalls || {})[arguments[0]];
if (!s) return {gone: true};
if (!s.done) return {done: false};
delete window.__acCalls[arguments[0]];
return s;
"""


class Relay:
    def __init__(self, browser: Browser) -> None:
        self.b = browser
        self.calls = 0
        self.seconds = 0.0

    def _ready(self) -> None:
        """The library needs a logged-in admin page of this site to run in."""
        cur = (self.b.d.current_url or "").lower()
        if self.b.host not in cur or "/admin" not in cur:
            self.b.get("/Admin/FormCenter")

    def call(self, mod: str, op: str, *args: Any, timeout: float = 900.0) -> Any:
        self.b.cancel.check()
        self._ready()
        t0 = time.monotonic()
        token = self.b._script_past_stray_alerts(_START, mod, op, list(args))
        wait = 0.03
        end = time.monotonic() + timeout
        while True:
            time.sleep(wait)
            wait = min(0.5, wait * 1.6)
            if self.b.cancel.cancelled:
                self.b.r.warn(f"Stopped. The request in progress ({mod} {op}) may still finish on the site; "
                              "read the module again to see where it stands.")
                raise Cancelled()
            st = self.b._script_past_stray_alerts(_POLL, token)
            if st.get("gone"):
                raise SiteError("The browser page changed during a request (did the tab navigate?). Run it again.")
            if st.get("done"):
                break
            if time.monotonic() > end:
                raise SiteError(f"{mod} {op} took longer than {timeout:.0f}s.")
        self.calls += 1
        self.seconds += time.monotonic() - t0
        if st.get("error"):
            if "logged out" in st["error"]:
                raise SiteError("The site logged us out. Log in again and re-run.")
            raise SiteError(st["error"])
        return st.get("value")

    # ------------------------------------------------------------ helpers
    def cats(self, mod: str) -> list[dict]:
        return self.call(mod, "cats") or []

    def items(self, mod: str, cid: int) -> list[dict]:
        return self.call(mod, "items", cid) or []

    def act(self, mod: str, level: str, action: str, ids: list[int], cid: int = 0,
            extra: Any = None) -> tuple[list[int], dict[int, str]]:
        """publish | unpublish | delete IDs, CHUNK at a time. level: category | item.
        Returns (done IDs, {failed ID: why})."""
        ok: list[int] = []
        failed: dict[int, str] = {}
        ids = [int(i) for i in ids]
        for i in range(0, len(ids), CHUNK):
            part = ids[i:i + CHUNK]
            if level == "category":
                args = [action, part] + ([extra] if extra is not None else [])
                r = self.call(mod, "catAct", *args)
            else:
                r = self.call(mod, "itemAct", action, cid, part)
            ok += [int(x) for x in r.get("ok", [])]
            failed.update({int(k): v for k, v in (r.get("failed") or {}).items()})
        return ok, failed

    def create(self, mod: str, name: str, publish: bool, *extra: Any) -> dict:
        return self.call(mod, "create", name, bool(publish), *extra) or {}
