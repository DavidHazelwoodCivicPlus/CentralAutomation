"""Browser prompts (alert / confirm) during the site steps.

Central's module pages answer with prompts that hold the page until someone
clicks: "Import results / 36 rows imported successfully." (Calendar),
"...wasn't provided... Proceed?" (Staff Directory), and so on. While one is
open, nothing else on the page answers. Every prompt is read, logged and
answered here: accepted, except anything about deleting, removing or
replacing, which is always refused.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional

REFUSE = re.compile(r"\b(delet|remov|eras|purg|overwrit|replace all|clear all|wipe)", re.I)


def decide(text: str) -> bool:
    """True to accept (OK), False to refuse (Cancel)."""
    return not REFUSE.search(text or "")


def answer(d, until: Callable[[], bool], timeout: float = 600, quiet: float = 2.0,
           log: Callable[[str], None] = lambda m: None) -> list[str]:
    """Answer every prompt until until() is true and no prompt has shown for
    `quiet` seconds (or timeout). Returns the prompts' texts, in order."""
    from selenium.webdriver.common.alert import Alert
    seen: list[str] = []
    end = time.time() + timeout
    calm_since: Optional[float] = None
    while time.time() < end:
        try:
            a = Alert(d)
            text = a.text or ""
            ok = decide(text)
            a.accept() if ok else a.dismiss()
            seen.append(text)
            log(f"  the site asked: {' '.join(text.split())[:200]}  -> {'OK' if ok else 'Cancel'}")
            calm_since = None
            continue
        except Exception:
            pass
        try:
            done = until()
        except Exception:               # mid-navigation, or a prompt just opened
            done = False
        if done:
            calm_since = calm_since or time.time()
            if time.time() - calm_since >= quiet:
                return seen
        else:
            calm_since = None
        time.sleep(0.4)
    return seen
