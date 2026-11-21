"""Editing page text blocks: find and replace rules, and the page editor that
makes the change on the site the way a person does in Live Edit.

The editor works in its own browser tab: open the page with Live Edit on,
click into the text block, switch to HTML view, put the new HTML in, press
Done Editing (the green check), then Save the page, then Publish it again if
it was published. Measured on training19 (2026-11-20): about 12-13 s for a
page with one changed block, about 5-7 s more for each further block.
"""

from __future__ import annotations

import html as _html
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .models import InventoryRow

_TAG = re.compile(r"(<[^>]*>)")


@dataclass
class Rule:
    """One find and replace."""

    find: str
    replace: str = ""
    in_html: bool = False          # False: only the visible text, never tags or addresses
    match_case: bool = False
    whole_word: bool = False
    regex: bool = False

    def pattern(self) -> re.Pattern:
        p = self.find if self.regex else re.escape(self.find)
        if self.whole_word:
            p = r"(?<!\w)" + p + r"(?!\w)"
        return re.compile(p, 0 if self.match_case else re.I)

    def describe(self) -> str:
        opts = [o for o, on in (("HTML", self.in_html), ("match case", self.match_case),
                                ("whole words", self.whole_word), ("regex", self.regex)) if on]
        return f'"{self.find}" -> "{self.replace}"' + (f" ({', '.join(opts)})" if opts else "")

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def apply_rule(html: str, rule: Rule) -> tuple[str, int]:
    """(new HTML, how many replacements). In text mode only the text between
    tags changes; tags, links and image addresses are left alone."""
    pat = rule.pattern()
    if rule.in_html:
        return pat.subn(rule.replace, html)
    total = 0
    parts = _TAG.split(html)
    for i in range(0, len(parts), 2):             # even pieces are text, odd ones tags
        seg = parts[i]
        if not seg:
            continue
        plain = _html.unescape(seg)
        new, n = pat.subn(rule.replace, plain)
        if n:
            parts[i] = _html.escape(new, quote=False)
            total += n
    return "".join(parts), total


def context(text: str, rule: Rule, width: int = 40) -> list[tuple[str, str]]:
    """Each match in a block's readable text: (before, after) with a little around it."""
    out = []
    pat = rule.pattern()
    for m in pat.finditer(text):
        a, b = max(0, m.start() - width), min(len(text), m.end() + width)
        left, right = text[a:m.start()], text[m.end():b]
        try:
            rep = m.expand(rule.replace) if rule.regex else rule.replace
        except re.error:
            rep = rule.replace
        before = ("…" if a else "") + left + m.group(0) + right + ("…" if b < len(text) else "")
        after = ("…" if a else "") + left + rep + right + ("…" if b < len(text) else "")
        out.append((before.replace("\n", " / "), after.replace("\n", " / ")))
    return out


@dataclass
class Match:
    page_id: int
    page: str
    block_id: str
    block: str            # the block's first line
    count: int
    before: str
    after: str
    shared: str = ""
    position: int = 0


def find_matches(rows: list[InventoryRow], rule: Rule, page_ids: Optional[set[int]] = None) -> list[Match]:
    """Every text block (on the given pages, or all) that the rule would change."""
    out = []
    pages = {r.category_id: r.name for r in rows if r.level == "category"}
    seen: dict[int, int] = {}
    for r in rows:
        if r.status != "text block":
            continue
        pos = seen.get(r.category_id, 0)
        seen[r.category_id] = pos + 1
        if page_ids is not None and r.category_id not in page_ids:
            continue
        html = r.fields.get("HTML", "")
        new, n = apply_rule(html, rule)
        if not n:
            continue
        hay = html if rule.in_html else r.fields.get("Text", "")
        ctx = context(hay, rule) or [("", "")]
        out.append(Match(r.category_id, pages.get(r.category_id, ""), r.extra.get("block_id", ""), r.name, n,
                         ctx[0][0], ctx[0][1], r.fields.get("Shared", ""), pos))
    return out


def plain(html: str) -> str:
    """The words of some HTML, for telling whether two versions of a block say the same."""
    # scripts and styles don't count: the page adds its own small script to blocks when it shows them
    html = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", html or "", flags=re.I | re.S)
    return " ".join(_html.unescape(_TAG.sub(" ", html)).split())


# ================================================================== the editor
class PageEditor:
    """Changes text blocks on pages through Live Edit, in its own tab of the
    tool's Chrome. One page at a time; Central answers one request at a time."""

    def __init__(self, browser, log: Callable[[str], None]) -> None:
        self.b = browser
        self.log = log
        self.home: Optional[str] = None

    # Selenium is imported here so the rest of the tool's tests don't need a browser.
    def __enter__(self) -> "PageEditor":
        d = self.b.d
        self.home = d.current_window_handle
        d.switch_to.new_window("tab")
        d.get(self.b.url("/Admin"))
        d.execute_script("document.cookie = 'enableLiveEdit=true; path=/';")
        return self

    def __exit__(self, *exc) -> None:
        d = self.b.d
        try:
            d.execute_script("document.cookie = 'enableLiveEdit=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';")
            d.close()
        except Exception:
            pass
        if self.home:
            d.switch_to.window(self.home)

    def _wait(self, fn, timeout: float = 30.0, what: str = ""):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            self.b.cancel.check()
            self.b.handle_session_timeout()
            try:
                v = fn()
                if v:
                    return v
            except Exception as e:          # element not there yet, page still drawing
                last = e
            time.sleep(0.2)
        raise TimeoutError(f"{what or 'the page'} didn't respond in {timeout:.0f}s" + (f" ({last})" if last else ""))

    def edit_page(self, page_id: int, change: Callable[[str, str, int], Optional[str]], publish: bool) -> dict:
        """change(block_id, current_html, position) returns the new HTML, or
        None to leave the block as it is. position counts the page's text
        blocks from 0: a block gets a new id each time the page is saved as a
        new version, so its position is how it's found again.
        Returns {"blocks": {id: (old, new, position)}, "error": str}."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By
        d = self.b.d
        done: dict[str, tuple[str, str]] = {}
        d.get(self.b.url(f"/{page_id}"))
        self._wait(lambda: d.execute_script("return document.readyState") == "complete", 60, "the page")
        wc = d.execute_script("""const v = document.getElementById('hdnVersionID'); if (!v) return 'none';
            const x = new XMLHttpRequest(); x.open('GET', '/Pages/Version/HasWorkingCopy?versionID=' + v.value, false); x.send();
            return x.responseText;""")
        if "true" in str(wc).lower():
            return {"blocks": {}, "error": "the page has unsaved Live Edit changes (a working copy); left alone"}
        ids = d.execute_script("""return [...document.querySelectorAll('[data-cprole=mainContentContainer] [id^=divEditor]')]
            .map(e => e.id.replace(/^divEditor/, ''));""") or []
        for pos, bid in enumerate(ids):
            current = d.execute_script("const e = document.getElementById('divEditor' + arguments[0]); return e ? e.innerHTML : null;", bid)
            if current is None or change(bid, current, pos) is None:
                continue
            el = d.find_element(By.ID, "divEditor" + bid)
            d.execute_script("arguments[0].scrollIntoView({block: 'center'})", el)
            ActionChains(d).move_to_element(el).pause(0.3).click().perform()
            self._wait(lambda: d.find_elements(By.CSS_SELECTOR, ".fr-toolbar button[data-cmd=html]"), 30, "the editor")
            btn = d.find_element(By.CSS_SELECTOR, ".fr-toolbar button[data-cmd=html]")
            ActionChains(d).move_to_element(btn).pause(0.2).click().perform()
            self._wait(lambda: d.execute_script("const c = document.querySelector('.CodeMirror'); return !!(c && c.CodeMirror);"),
                       20, "the HTML view")
            time.sleep(0.4)
            old = d.execute_script("return document.querySelector('.CodeMirror').CodeMirror.getValue();")
            new = change(bid, old, pos)
            if new is None or new == old:
                ActionChains(d).move_to_element(btn).pause(0.2).click().perform()
            else:
                d.execute_script("document.querySelector('.CodeMirror').CodeMirror.setValue(arguments[0]);", new)
                time.sleep(0.4)
                ActionChains(d).move_to_element(btn).pause(0.2).click().perform()     # back to the normal view
                done[bid] = (old, new, pos)
            time.sleep(0.4)
            ok = d.execute_script("""return [...document.querySelectorAll('a,button')].find(b => /^done editing$/i.test(b.textContent.trim()) && b.offsetParent)""")
            if ok is None:
                return {"blocks": done, "error": "couldn't find Done Editing (the green check)"}
            ActionChains(d).move_to_element(ok).pause(0.2).click().perform()
            self._wait(lambda: d.execute_script("const t = document.querySelector('.fr-toolbar'); return !t || !t.offsetParent;"),
                       60, "Done Editing")
            time.sleep(1.0)
        if not done:
            return {"blocks": {}, "error": ""}
        save = self._wait(lambda: d.find_element(By.ID, "lnkVersionSave"), 30, "the page's Save")
        d.execute_script("arguments[0].scrollIntoView({block: 'center'})", save)
        ActionChains(d).move_to_element(save).pause(0.2).click().perform()
        self._wait(lambda: d.execute_script("return document.readyState") == "complete", 60, "Save")
        time.sleep(1.5)
        if publish:
            r = d.execute_script("""const v = document.getElementById('hdnVersionID').value;
                const x = new XMLHttpRequest(); x.open('POST', '/Pages/VersionStatus/Publish/' + v + '?pageID=' + arguments[0] + '&workingCopyAction=1', false);
                x.setRequestHeader('X-Requested-With', 'XMLHttpRequest'); x.send(); return x.status + ' ' + x.responseText.slice(0, 120);""", page_id)
            if not str(r).startswith("200") or "ErrorMessage" in str(r):
                return {"blocks": done, "error": f"saved, but publishing again failed ({r})"}
        return {"blocks": done, "error": ""}
