"""Everything the tool knows about the Agenda Center admin pages.

Read operations use fetch() inside the logged-in browser (fast, changes
nothing). Write operations click the real controls and then re-read the
site to confirm what actually happened.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from selenium.common.exceptions import (StaleElementReferenceException, TimeoutException,
                                        UnexpectedAlertPresentException)
from selenium.webdriver.common.by import By

from ..browser import UPLOAD_WAIT, WAIT, Browser, SiteError
from .models import (Agenda, AgendaDetails, Category, Folder, ImportItem, Item, Meeting, Workflow,
                     parse_site_date, site_date)

AGENDA_CENTER = "/Admin/AgendaCenter"

# View Options that must be in this state or scans silently miss agendas.
REQUIRED_VIEW_OPTIONS = {
    "DisplayAgendasOnlyMine": False,
    "DisplayAgendasPublished": True,
    "DisplayAgendasDraft": True,
    "DisplayAgendasSubmitted": True,
    "DisplayAgendasDeclined": True,
    "DisplayCategoriesPublished": True,
    "DisplayCategoriesDraft": True,
    "DisplayCategoriesSubmitted": True,
    "DisplayCategoriesDeclined": True,
    "DisplayItemsOnlyMine": False,
    "DisplayItemsDraft": True,
    "DisplayItemsSubmittedForApproval": True,
    "DisplayItemsApproved": True,
    "DisplayItemsDeclined": True,
}

_PARSE_CATEGORIES = """
    return [...doc.querySelectorAll('#divCategories .item')].map(item => {
        const cb = item.querySelector('input.check');
        const a = item.querySelector('h3 a');
        if (!cb || !a) return null;
        return {id: cb.value, name: a.textContent.trim(), published: cb.name === 'cboxPublishedCategory'};
    }).filter(Boolean);
"""

# Items tab rows have a checkbox; Default Items rows have only a hidden ItemID
# (no bulk actions there). Folder rows sit in the Items list too.
_PARSE_ITEM_LIST = """
    const rows = [...doc.querySelectorAll('.row.item')].filter(r => !r.classList.contains('folderRow')).map(r => {
        const idEl = r.querySelector('input.check') || r.querySelector('input.itemID, input[name="ItemID"]');
        const name = r.querySelector('p.name');
        const st = r.querySelector('span.status');
        const folder = r.querySelector('input.folderID');
        return {id: idEl ? idEl.value : null,
                name: name ? (name.getAttribute('title') || name.innerText.trim()) : '',
                date: (r.querySelector('p.date') || {innerText: ''}).innerText.trim(),
                status: st ? st.className.replace('status', '').trim() : '',
                folder: folder && folder.value ? folder.value : '0'};
    }).filter(x => x.id);
    const folders = [...doc.querySelectorAll('.row.folderRow')].map(r => {
        const id = (r.querySelector('input.folderID') || {value: ''}).value;
        return {id, name: (r.querySelector('p.folder') || {innerText: ''}).innerText.trim(),
                items: doc.querySelectorAll('.itemRow.folder' + id).length};
    }).filter(f => f.id && f.id !== '0');
    const pages = [...doc.querySelectorAll('[data-pagenum]')].map(e => parseInt(e.dataset.pagenum, 10)).filter(n => !isNaN(n));
    return {rows, folders, maxPage: pages.length ? Math.max(...pages) : 0};
"""

_PARSE_WORKFLOWS = """
    return [...doc.querySelectorAll('.rows .item')].map(r => {
        const a = r.querySelector('h3 a');
        const m = a ? (a.getAttribute('href') || '').match(/Workflows\\/Edit\\/(\\d+)/) : null;
        return m ? {id: m[1], name: a.textContent.trim()} : null;
    }).filter(Boolean);
"""

# Category page tab -> (tab link id, panel id)
_TABS = {"Agendas": ("lnkAgendas", "divAgendas"), "Items": ("lnkItems", "divItems"),
         "DefaultItems": ("lnkDefaultItems", "divDefaultItems"), "Workflow": ("lnkWorkflow", "divWorkflow")}

_PARSE_AGENDA_LIST = """
    const rows = [...doc.querySelectorAll('.row.item')].map(r => {
        const cb = r.querySelector('input.check');
        const dates = r.querySelectorAll('p.date');
        const st = r.querySelector('span.status');
        const minutes = r.querySelector('a.minutes');
        const m = minutes ? (minutes.getAttribute('href') || '').match(/Minutes\\/Edit\\/(\\d+)/) : null;
        return {
            id: cb ? cb.value : null,
            published: cb ? cb.name === 'cboxPublishedAgenda' : false,
            date: dates[0] ? dates[0].innerText.trim() : '',
            updated: dates[1] ? dates[1].innerText.trim() : '',
            desc: (r.querySelector('p.desc') || {innerText: ''}).innerText.trim(),
            statusClass: st ? st.className : '',
            minutesId: m ? parseInt(m[1], 10) : null,
        };
    }).filter(x => x.id);
    const pages = [...doc.querySelectorAll('[data-pagenum]')].map(e => parseInt(e.dataset.pagenum, 10)).filter(n => !isNaN(n));
    return {rows, maxPage: pages.length ? Math.max(...pages) : 1};
"""


def _item_status(cls: str) -> str:
    cls = (cls or "").lower()
    for s in ("approved", "submitted", "declined", "draft"):
        if s in cls:
            return s
    return "pending" if "pending" in cls else (cls or "unknown")


def _status_from_class(cls: str, published: bool) -> str:
    for s in ("draft", "submitted", "declined"):
        if s in (cls or ""):
            return s
    return "published" if published else "draft"


class AgendaCenterSite:
    def __init__(self, browser: Browser) -> None:
        self.b = browser
        self.r = browser.r
        self._categories: Optional[list[Category]] = None

    # ================================================================ reading
    def categories(self, refresh: bool = False) -> list[Category]:
        if self._categories is None or refresh:
            html = self.b.fetch_text(AGENDA_CENTER)
            data = self.b.run_parser(_PARSE_CATEGORIES, html) or []
            self._categories = [Category(int(c["id"]), c["name"], bool(c["published"])) for c in data]
        return self._categories

    def find_category(self, name: str, refresh: bool = False) -> Optional[Category]:
        key = name.strip().lower()
        for c in self.categories(refresh):
            if c.name.strip().lower() == key:
                return c
        return None

    def agendas(self, category: Category) -> list[Agenda]:
        """Every agenda in a category, all pages, via the list endpoint."""
        out: list[Agenda] = []
        page, max_page = 1, 1
        while page <= max_page:
            html = self.b.fetch_text(f"{AGENDA_CENTER}/Agenda/AgendaList?categoryID={category.id}&page={page}")
            data = self.b.run_parser(_PARSE_AGENDA_LIST, html) or {"rows": [], "maxPage": 1}
            max_page = max(max_page, int(data.get("maxPage") or 1))
            for row in data["rows"]:
                out.append(Agenda(
                    id=int(row["id"]),
                    category_id=category.id,
                    category_name=category.name,
                    meeting_date=parse_site_date(row["date"]),
                    updated=parse_site_date(row["updated"]),
                    description=row["desc"],
                    published=bool(row["published"]),
                    status=_status_from_class(row["statusClass"], bool(row["published"])),
                    has_minutes=bool(row["minutesId"]),
                    minutes_id=int(row["minutesId"] or 0),
                ))
            page += 1
            if page > 400:
                raise SiteError("Agenda list paging did not end; stopping to be safe.")
        # The list can repeat a row across pages if something changes mid-scan.
        seen: set[int] = set()
        unique = []
        for a in out:
            if a.id not in seen:
                seen.add(a.id)
                unique.append(a)
        return unique

    def details(self, agenda: Agenda) -> AgendaDetails:
        """Open-the-edit-page facts (about 1 second each). Cached on the agenda."""
        if agenda.details is not None:
            return agenda.details
        html = self.b.fetch_text(f"{AGENDA_CENTER}/Agenda/Edit/{agenda.id}?categoryID={agenda.category_id}")

        def field(name: str) -> str:
            m = re.search(r'name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', html) or \
                re.search(r'value="([^"]*)"[^>]*name="' + re.escape(name) + r'"', html)
            return _unescape(m.group(1)) if m else ""

        # On drafts Agenda.IsSimpleAgenda is a checkbox (value "true" whether
        # ticked or not) plus a hidden "false"; on published agendas it is a
        # hidden field holding True/False.
        chk = re.search(r'<input[^>]*id="chkSimpleAgenda"[^>]*>', html)
        if chk and 'name="Agenda.IsSimpleAgenda"' in chk.group(0):
            simple_raw = "true" if re.search(r'\schecked\b', chk.group(0)) else "false"
        else:
            simple_raw = field("Agenda.IsSimpleAgenda")
        if not simple_raw:
            raise SiteError(f"Could not read agenda {agenda.id}'s edit page (logged out or page changed?).")
        item_files = [_unescape(f) for f in re.findall(r'\\?"FileName\\?"\s*:\s*\\?"([^"\\]*)', html)]
        det = AgendaDetails(
            is_simple=simple_raw.lower() == "true",
            agenda_file=field("Agenda.AgendaFile"),
            minutes_file=field("Agenda.Minutes.MinutesFile"),
            minutes_id=int(field("Agenda.Minutes.MinutesID") or 0) or agenda.minutes_id,
            item_count=len(re.findall(r'name="agendaItems\.index"', html)),
            item_files=item_files,
            minutes_status=field("Agenda.Minutes.Status"),
        )
        if not det.is_simple:
            det.agenda_file = ""   # auto-generated PDF name, not an uploaded file
        if det.minutes_id and not det.minutes_file:
            # Agendas not made with "Upload an agenda" keep their minutes on
            # the minutes page only.
            mfile, mstatus = self.minutes_info(agenda.id, det.minutes_id)
            det.minutes_file, det.minutes_status = mfile, mstatus or det.minutes_status
        agenda.details = det
        return det

    def minutes_info(self, agenda_id: int, minutes_id: int) -> tuple[str, str]:
        html = self.b.fetch_text(f"{AGENDA_CENTER}/Minutes/Edit/{minutes_id}?agendaID={agenda_id}")

        def field(name: str) -> str:
            m = re.search(r'name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', html) or \
                re.search(r'value="([^"]*)"[^>]*name="' + re.escape(name) + r'"', html)
            return _unescape(m.group(1)) if m else ""
        return field("Minutes.MinutesFile"), field("Minutes.Status")

    # ========================================================== view options
    def ensure_view_options(self, category: Category) -> None:
        """Make sure no saved View Option hides drafts, published, etc."""
        self.b.get_with_retry(f"{AGENDA_CENTER}/Category/Detail/{category.id}", "#divCategoryHeader")
        wrong = self._wrong_view_options()
        if not wrong:
            return
        self.r.warn("Saved View Options were hiding some agendas/categories: " + ", ".join(wrong) + ". Fixing.")
        self.b.safe_click(self.b.d.find_element(By.ID, "btnViewOptions"))
        self.b.wait_until(lambda: self.b.d.find_element(By.ID, "applyChanges").is_displayed())
        for opt in wrong:
            self.b.safe_click(self.b.d.find_element(By.ID, opt))
        self.b.safe_click(self.b.d.find_element(By.ID, "applyChanges"))
        self.b.sleep(2)
        self.b.get_with_retry(f"{AGENDA_CENTER}/Category/Detail/{category.id}", "#divCategoryHeader")
        still = self._wrong_view_options()
        if still:
            raise SiteError("Could not fix View Options (" + ", ".join(still) + "). Fix them by hand and re-run.")

    def _wrong_view_options(self) -> list[str]:
        wrong = []
        for opt, want in REQUIRED_VIEW_OPTIONS.items():
            els = self.b.d.find_elements(By.ID, opt)
            if els and els[0].is_selected() != want:
                wrong.append(opt)
        return wrong

    # ===================================================== agenda bulk actions
    def open_agendas_tab(self, category: Category) -> None:
        self.b.get_with_retry(f"{AGENDA_CENTER}/Category/Detail/{category.id}", "#divCategoryHeader")
        self.b.safe_click(self.b.d.find_element(By.ID, "lnkAgendas"))
        self.b.wait_until(lambda: len(self.b.d.find_elements(By.CSS_SELECTOR, "#divAgendas input.noLoaded")) == 0
                          and len(self.b.d.find_elements(By.CSS_SELECTOR, "#divAgendas .head")) > 0, timeout=30)
        self._wait_list_stable()

    def _wait_list_stable(self, quiet: float = 1.5, timeout: float = 30) -> None:
        """The page can load the agenda list more than once (its own tab
        restore plus our click). Ticks made on a list that then gets replaced
        are silently lost, so wait until the list stops being redrawn."""
        end = time.time() + timeout
        while time.time() < end:
            token = self._mark_rows()
            time.sleep(quiet)
            still = self.b.d.execute_script(
                "return !!document.querySelector('[data-ac-mark=\"' + arguments[0] + '\"]');", token)
            loading = self.b.d.find_elements(By.CSS_SELECTOR, "#divAgendas input.noLoaded")
            if still and not loading:
                return
        raise SiteError("The agenda list kept reloading; could not get a stable page.")

    def _ticked(self) -> set[int]:
        return set(int(v) for v in self.b.d.execute_script(
            "return [...document.querySelectorAll('#divAgendas .rows .row.item input.check:checked')]"
            ".map(c => c.value);"))

    def _rows_on_page(self) -> dict[int, bool]:
        """{agenda_id: is_published} for rows currently shown."""
        data = self.b.d.execute_script("""
            return [...document.querySelectorAll('#divAgendas .rows .row.item input.check')]
                .map(cb => [cb.value, cb.name === 'cboxPublishedAgenda']);
        """) or []
        return {int(i): bool(p) for i, p in data}

    def _page_count(self) -> int:
        nums = self.b.d.execute_script("""
            return [...document.querySelectorAll('#paginationAgendas [data-pagenum]')]
                .map(e => parseInt(e.dataset.pagenum, 10)).filter(n => !isNaN(n));
        """) or []
        return max(nums) if nums else 1

    def _go_to_page(self, n: int) -> None:
        marker = self._mark_rows()
        self.b.d.execute_script("changePage('paginationAgendas', arguments[0]);", n)
        self._wait_rows_replaced(marker)

    def _mark_rows(self) -> str:
        token = f"ac{time.time_ns()}"
        self.b.d.execute_script(
            "const el = document.querySelector('#divAgendas .rows') || document.querySelector('#divAgendas');"
            "if (el) el.setAttribute('data-ac-mark', arguments[0]);", token)
        return token

    def _wait_rows_replaced(self, token: str, timeout: float = 60) -> None:
        def replaced():
            still = self.b.d.execute_script(
                "return !!document.querySelector('[data-ac-mark=\"' + arguments[0] + '\"]');", token)
            loading = self.b.d.find_elements(By.CSS_SELECTOR, "#divAgendas input.noLoaded")
            return (not still) and not loading
        self.b.wait_until(replaced, timeout=timeout)
        self.b.check_for_problems()

    def bulk_agenda_action(self, category: Category, agenda_ids: Iterable[int], action: str,
                           batch_done=None) -> dict[int, str]:
        """Unpublish / delete / publish the given agendas in one category using
        the category page's Bulk Actions (up to 25 rows per click).

        Returns {agenda_id: "ok" | error text}. Each batch is confirmed by
        re-reading the category from the site, not by trusting the page.
        """
        assert action in ("unpublish", "delete", "publish")
        link_id = {"unpublish": "lnkUnpublish", "delete": "lnkDelete", "publish": "lnkPublish"}[action]
        remaining = set(int(i) for i in agenda_ids)
        results: dict[int, str] = {}
        stalls = 0

        while remaining:
            self.b.cancel.check()
            self.open_agendas_tab(category)
            batch: list[int] = []
            pages = self._page_count()
            for page in range(1, pages + 1):
                if page > 1:
                    self._go_to_page(page)
                on_page = self._rows_on_page()
                batch = [i for i, pub in on_page.items() if i in remaining and self._eligible(action, pub)]
                if batch:
                    break
            if not batch:
                # Nothing left that this action can apply to.
                for i in remaining:
                    results[i] = "not found on the category page in a state this action applies to"
                break

            for agenda_id in batch:
                cb = self.b.d.find_element(By.CSS_SELECTOR,
                                           f'#divAgendas .rows .row.item input.check[value="{agenda_id}"]')
                if not cb.is_selected():
                    self.b.safe_click(cb)
            if not self.b.d.find_element(By.ID, link_id).is_displayed():
                self.b.safe_click(self.b.d.find_element(By.ID, "btnBulkAction"))
                self.b.wait_until(lambda: self.b.d.find_element(By.ID, link_id).is_displayed())
            link = self.b.d.find_element(By.ID, link_id)
            if "inactive" in (link.get_attribute("class") or ""):
                raise SiteError(f"The bulk {action} button is disabled on this page.")

            # Mark the list, give it a moment, and re-check the ticks right
            # before clicking: if the list was redrawn in between, start over.
            marker = self._mark_rows()
            time.sleep(0.8)
            ticked = self._ticked()
            if ticked != set(batch):
                self.r.warn(f"Selection changed before {action} (list redrawn?); retrying this batch.")
                stalls += 1
                if stalls >= 3:
                    raise SiteError(f"Could not keep rows {sorted(batch)} ticked; stopping.")
                continue
            self.b.safe_click(link)
            if action == "delete":
                self.b.answer_confirm(["delete"])
            if action == "publish":
                if not self.b.answer_notify_dont_send(precheck=lambda: self._ticked() == set(batch)):
                    self.r.warn("Notify Me pop-up did not appear for this publish (category without "
                                "subscribers?); checking the result.")
            alert = self.b.take_alert(1.0)
            if alert is not None:
                self.b.dismiss_any_alert()
                raise SiteError(f"Site said: {alert}")
            try:
                self._wait_rows_replaced(marker, timeout=90)
            except TimeoutException:
                self.r.warn("Page did not visibly refresh after the bulk action; checking the site directly.")

            # Confirm against the site itself.
            current = {a.id: a for a in self.agendas(category)}
            progressed = 0
            for agenda_id in batch:
                a = current.get(agenda_id)
                done = (a is None) if action == "delete" else (a is not None and a.published == (action == "publish"))
                if done:
                    results[agenda_id] = "ok"
                    remaining.discard(agenda_id)
                    progressed += 1
            if batch_done:
                batch_done([i for i in batch if results.get(i) == "ok"])
            if progressed == 0:
                stalls += 1
                if stalls >= 2:
                    for i in batch:
                        results.setdefault(i, f"{action} did not take effect")
                        remaining.discard(i)
                    stalls = 0
            else:
                stalls = 0
        return results

    @staticmethod
    def _eligible(action: str, published: bool) -> bool:
        return (action == "unpublish" and published) or (action in ("delete", "publish") and not published)

    # ============================================================= categories
    def _category_menu_link(self, category: Category, css_class: str):
        links = self.b.d.find_elements(By.CSS_SELECTOR, f"#categoryMenu_{category.id} a.{css_class}")
        return links[0] if links else None

    def category_action(self, category: Category, action: str) -> str:
        """publish | unpublish | delete one category via its Actions menu."""
        self.b.get_with_retry(f"{AGENDA_CENTER}/Category/Detail/{category.id}", "#divCategoryHeader")
        link = self._category_menu_link(category, action)
        if link is None:
            if action == "delete" and self._category_menu_link(category, "unpublish") is not None:
                return "category is published; unpublish it first"
            return f"no '{action}' option in this category's Actions menu (already in that state?)"
        self.b.safe_click(link)
        if action == "delete":
            text = self.b.take_alert(10)
            if text is None:
                return "no confirmation appeared"
            if "may not delete" in text.lower() or "cannot" in text.lower():
                self.b.dismiss_any_alert()
                return f"site refused: {text}"
            self.b.answer_confirm(["delete"], timeout=1)
            self.b.wait_until(lambda: "/Category/Detail/" not in self.b.d.current_url, timeout=30)
            gone = self.find_category(category.name, refresh=True) is None
            return "ok" if gone else "category still exists after delete"
        if action == "publish":
            self.b.answer_notify_dont_send(appear_timeout=3)
        alert = self.b.take_alert(1.5)
        if alert is not None:
            self.b.dismiss_any_alert()
            return f"site said: {alert}"

        def state_ok():
            c = next((x for x in self.categories(refresh=True) if x.id == category.id), None)
            return c is not None and c.published == (action == "publish")
        try:
            self.b.wait_until(state_ok, timeout=30, poll=2)
            return "ok"
        except TimeoutException:
            return f"category did not show as {action}ed"

    def create_category(self, name: str, publish: bool) -> tuple[Optional[Category], str]:
        existing = self.find_category(name, refresh=True)
        if existing:
            return existing, "already exists"
        self.b.get_with_retry(f"{AGENDA_CENTER}/Category/Edit/0", "#txtTitle")
        # The page's own start-up script empties Name if we type too soon
        # (seen live 2026-11-17): type, then check it's still there.
        for _ in range(5):
            self.b.sleep(1.0)
            title = self.b.d.find_element(By.ID, "txtTitle")
            title.clear()
            title.send_keys(name)
            self.b.sleep(1.0)
            if self.b.d.find_element(By.ID, "txtTitle").get_attribute("value") == name:
                break
        else:
            return None, "the Name box kept clearing; nothing saved"
        wanted = "saveChanges('publish')" if publish else "saveChanges('save')"
        btn = next((a for a in self.b.d.find_elements(By.CSS_SELECTOR, "a.button")
                    if wanted in (a.get_attribute("onclick") or "")), None)
        if btn is None:
            return None, "could not find the save button"
        self.b.safe_click(btn)
        if publish:
            self.b.answer_notify_dont_send(appear_timeout=3)
        alert = self.b.take_alert(1.5)
        if alert is not None:
            self.b.dismiss_any_alert()
            return None, f"site said: {alert}"
        self.b.wait_until(lambda: re.search(r"/Category/Detail/\d+", self.b.d.current_url), timeout=60)
        cat = self.find_category(name, refresh=True)
        return (cat, "ok") if cat else (None, "saved but the category is not in the list")

    # ============================================ items, folders and workflows
    def open_tab(self, category: Category, tab: str) -> None:
        """Open a category page tab (Items, DefaultItems, Workflow) and wait
        until its panel has loaded and stopped redrawing."""
        link_id, div = _TABS[tab]
        self.b.get_with_retry(f"{AGENDA_CENTER}/Category/Detail/{category.id}", "#divCategoryHeader")
        self.b.safe_click(self.b.d.find_element(By.ID, link_id))
        self.b.wait_until(lambda: not self.b.d.find_elements(By.CSS_SELECTOR, f"#{div} input.noLoaded")
                          and self.b.d.find_element(By.ID, div).is_displayed(), timeout=30)
        end = time.time() + 30
        while time.time() < end:
            token = self._mark_panel(div)
            time.sleep(1.2)
            if self._panel_has_mark(token) and not self.b.d.find_elements(By.CSS_SELECTOR, f"#{div} input.noLoaded"):
                return
        raise SiteError(f"The {tab} list kept reloading; could not get a stable page.")

    def _mark_panel(self, div: str) -> str:
        """Tag the panel's first child; the site's redraw replaces the panel's
        HTML, so the tag disappearing means the action came back."""
        token = f"ac{time.time_ns()}"
        self.b.d.execute_script(
            "const el = document.querySelector('#' + arguments[0] + ' > *') || document.getElementById(arguments[0]);"
            "el.setAttribute('data-ac-mark', arguments[1]);", div, token)
        return token

    def _panel_has_mark(self, token: str) -> bool:
        return bool(self.b.d.execute_script(
            "return !!document.querySelector('[data-ac-mark=\"' + arguments[0] + '\"]');", token))

    def _wait_panel_redrawn(self, token: str, timeout: float = 60) -> None:
        try:
            self.b.wait_until(lambda: not self._panel_has_mark(token), timeout=timeout)
        except TimeoutException:
            self.r.warn("List did not visibly refresh after the action; checking the site directly.")
        self.b.check_for_problems()

    def bulk_item_action(self, category: Category, item_ids: Iterable[int], action: str) -> dict[int, str]:
        """approve | delete Items-tab items with the Items tab's Bulk Actions.
        Returns {item_id: "ok" | error text}, confirmed by re-reading the site."""
        assert action in ("approve", "delete")
        link_id = {"approve": "lnkApproveItem", "delete": "lnkDeleteItem"}[action]
        d = self.b.d
        remaining = set(int(i) for i in item_ids)
        results: dict[int, str] = {}
        stalls = 0
        row_js = """return [...document.querySelectorAll('#divItems .itemRow input.check[name="cboxSubmittedItems"]')]
            .map(cb => [cb.value, !!cb.closest('.item').querySelector('a.approve, a.submit')]);"""
        while remaining:
            self.b.cancel.check()
            self.open_tab(category, "Items")
            pages = sorted(set(int(n) for n in d.execute_script(
                "return [...document.querySelectorAll('#paginationItems [data-pagenum]')]"
                ".map(e => e.dataset.pagenum);") or [] if str(n).isdigit()))
            batch: list[int] = []
            for page in [None] + pages:
                if page is not None:
                    token = self._mark_panel("divItems")
                    d.execute_script("changePage('paginationItems', arguments[0]);", page)
                    self._wait_panel_redrawn(token)
                rows = {int(i): can_approve for i, can_approve in d.execute_script(row_js) or []}
                batch = [i for i, ok in rows.items() if i in remaining and (action == "delete" or ok)]
                if batch:
                    break
            if not batch:
                for i in remaining:
                    results[i] = "not found on the Items tab in a state this action applies to"
                break
            for iid in batch:
                cb = d.find_element(By.CSS_SELECTOR, f'#divItems input.check[name="cboxSubmittedItems"][value="{iid}"]')
                if not cb.is_selected():
                    self.b.safe_click(cb)
            if not d.find_element(By.ID, link_id).is_displayed():
                self.b.safe_click(d.find_element(By.ID, "btnBulkAction"))
                self.b.wait_until(lambda: d.find_element(By.ID, link_id).is_displayed())
            ticked = set(int(v) for v in d.execute_script(
                "return [...document.querySelectorAll('#divItems input.check[name=\"cboxSubmittedItems\"]:checked')]"
                ".map(c => c.value);"))
            if ticked != set(batch):
                stalls += 1
                if stalls >= 3:
                    raise SiteError(f"Could not keep items {sorted(batch)} ticked; stopping.")
                self.r.warn(f"Selection changed before {action} (list redrawn?); retrying.")
                continue
            token = self._mark_panel("divItems")
            self.b.safe_click(d.find_element(By.ID, link_id))
            if action == "delete":
                self.b.answer_confirm(["delete the selected items"], timeout=15)
            alert = self.b.take_alert(1.5)
            if alert is not None:
                self.b.dismiss_any_alert()
                raise SiteError(f"Site said: {alert}")
            self._wait_panel_redrawn(token, timeout=90)

            current = {i.id: i for i in self.items(category)}
            progressed = 0
            for iid in batch:
                it = current.get(iid)
                if (it is None) if action == "delete" else (it is not None and it.status == "approved"):
                    results[iid] = "ok"
                    remaining.discard(iid)
                    progressed += 1
            if progressed == 0:
                stalls += 1
                if stalls >= 2:
                    for iid in batch:
                        st = current.get(iid)
                        results.setdefault(iid, f"{action} did not take effect"
                                           + (f" (status {st.status})" if st and action == "approve" else ""))
                        remaining.discard(iid)
                    stalls = 0
            else:
                stalls = 0
        return results

    def _row_menu_action(self, category: Category, tab: str, menu_id: str, action: str,
                         confirm: Optional[str]) -> str:
        """Click one row's Actions-menu entry (the site binds them by id, so a
        click works without opening the menu) and answer its confirmation."""
        div = _TABS[tab][1]
        self.open_tab(category, tab)
        links = self.b.d.find_elements(By.CSS_SELECTOR, f"#{menu_id} a.{action}")
        if not links:
            return f"no '{action}' option in this row's Actions menu"
        token = self._mark_panel(div)
        self.b.safe_click(links[0])
        if confirm:
            self.b.answer_confirm([confirm], timeout=15)
        alert = self.b.take_alert(1.5)
        if alert is not None:
            self.b.dismiss_any_alert()
            return f"site said: {alert}"
        self._wait_panel_redrawn(token)
        return "ok"

    def item_action(self, category: Category, item: Item, action: str) -> str:
        """approve | delete one item from its own Actions menu (the only way
        for default items, which have no bulk actions)."""
        assert action in ("approve", "delete")
        tab = "DefaultItems" if item.default else "Items"
        st = self._row_menu_action(category, tab, f"itemMenu_{item.id}", action,
                                   "delete this item" if action == "delete" else None)
        if st != "ok":
            return st
        now = next((i for i in self.items(category, default=item.default) if i.id == item.id), None)
        if action == "delete":
            return "ok" if now is None else "item is still there after delete"
        return "ok" if now is not None and now.status == "approved" else \
            f"item status is {now.status if now else 'gone'!r}, not approved"

    def delete_folder(self, category: Category, folder: Folder) -> str:
        st = self._row_menu_action(category, "Items", f"folderMenu_{folder.id}", "delete", "delete this folder")
        if st != "ok":
            return st
        return "ok" if all(f.id != folder.id for f in self.folders(category)) else "folder is still there after delete"

    def delete_workflow(self, category: Category, wf: Workflow) -> str:
        st = self._row_menu_action(category, "Workflow", f"workflowMenu_{wf.id}", "delete", "delete this workflow")
        if st != "ok":
            return st
        return "ok" if all(w.id != wf.id for w in self.workflows(category)) else "workflow is still there after delete"

    # ================================================================ uploads
    def create_agenda(self, category: Category, meeting: Meeting,
                      resolve: Callable[[str], Path]) -> tuple[Optional[int], str]:
        """Create one agenda as a DRAFT. Publishing happens afterwards in bulk
        so the Notify Me pop-up is answered once per batch, not per agenda.

        simple       -> 'Upload an agenda' form with the agenda (+ minutes) file
        minutes_only -> bare agenda (name + date). The site only offers
                        Add Minutes once an agenda is published, so the
                        caller publishes it and then calls add_minutes().
        empty        -> bare agenda only
        complex      -> items created and approved, then placed in a
                        builder agenda (see _create_complex)
        """
        kind = meeting.kind
        if kind == "complex":
            return self._create_complex(category, meeting, resolve)

        before = {a.id for a in self.agendas(category)}
        self.b.get_with_retry(f"{AGENDA_CENTER}/Agenda/Edit/0?categoryID={category.id}", "#txtMeetingDate")
        d = self.b.d

        self._set_meeting_date(site_date(meeting.meeting_date))

        if kind == "simple":
            chk = d.find_element(By.ID, "chkSimpleAgenda")
            if not chk.is_selected():
                self.b.safe_click(chk)
                self.b.answer_confirm(["items that you have saved"])
            self.b.wait_until(lambda: d.find_element(By.ID, "simpleAgendaFile").is_displayed())

        desc = d.find_element(By.ID, "txtDesc")
        desc.clear()
        desc.send_keys(meeting.description)
        if meeting.video_link:
            d.find_element(By.ID, "txtVideoSource").send_keys(meeting.video_link)
        if meeting.additional_video_link:
            d.find_element(By.ID, "txtAdditionalVideoSource").send_keys(meeting.additional_video_link)

        if kind == "simple":
            d.find_element(By.ID, "simpleAgendaFile").send_keys(str(resolve(meeting.agenda_file).resolve()))
            if meeting.minutes_file:
                d.find_element(By.ID, "simpleMinutesFile").send_keys(str(resolve(meeting.minutes_file).resolve()))

        start_url = d.current_url
        self.b.safe_click(d.find_element(By.ID, "btnSaveAgenda"))
        new_id = self._wait_for_agenda_saved(start_url, category, before, meeting)
        if new_id is None:
            return None, "save did not complete"
        return new_id, "ok"

    # ------------------------------------------------------- complex agendas
    def _item_pages(self, category: Category, default: bool) -> tuple[list[dict], list[dict]]:
        rows: dict[int, dict] = {}
        folders: dict[int, dict] = {}
        page, max_page = 0, 0
        while True:
            html = self.b.fetch_text(f"{AGENDA_CENTER}/Item/ItemList?categoryID={category.id}"
                                     f"&defaultItems={default}&page={page}")
            data = self.b.run_parser(_PARSE_ITEM_LIST, html) or {"rows": [], "folders": [], "maxPage": 0}
            max_page = max(max_page, int(data.get("maxPage") or 0))
            new = 0
            for row in data["rows"]:
                if int(row["id"]) not in rows:
                    rows[int(row["id"])] = row
                    new += 1
            for f in data.get("folders") or []:
                folders.setdefault(int(f["id"]), f)
            page += 1
            # Default Items is one unpaged list.
            if default or (page > max_page and new == 0) or page > 200:
                break
        return list(rows.values()), list(folders.values())

    def items(self, category: Category, default: bool = False) -> list[Item]:
        """Items in a category that are not on an agenda (Items tab), or its
        default items (Default Items tab)."""
        rows, _ = self._item_pages(category, default)
        return [Item(id=int(r["id"]), category_id=category.id, category_name=category.name, name=r["name"],
                     status=_item_status(r["status"]), default=default,
                     meeting_date=None if default else parse_site_date(r["date"]),
                     folder_id=int(r.get("folder") or 0)) for r in rows]

    def folders(self, category: Category) -> list[Folder]:
        """Item folders on the Items tab (item_count = items shown inside)."""
        _, folders = self._item_pages(category, False)
        return [Folder(int(f["id"]), category.id, f["name"], int(f["items"])) for f in folders]

    def workflows(self, category: Category) -> list[Workflow]:
        html = self.b.fetch_text(f"{AGENDA_CENTER}/Workflows/WorkflowList?categoryID={category.id}")
        return [Workflow(int(w["id"]), category.id, w["name"]) for w in (self.b.run_parser(_PARSE_WORKFLOWS, html) or [])]

    def contents(self, category: Category) -> dict[str, list]:
        """Everything that stops the site deleting a category."""
        return {"agendas": self.agendas(category), "items": self.items(category),
                "default_items": self.items(category, default=True), "folders": self.folders(category),
                "workflows": self.workflows(category)}

    def create_item(self, category: Category, item: ImportItem, meeting_date,
                    resolve: Callable[[str], Path]) -> tuple[Optional[int], str]:
        """Create one agenda item (name, meeting date/time, description,
        files) with Save and Approve, so the agenda builder offers it."""
        name = item.name or "Item"
        before = {i.id for i in self.items(category)}
        self.b.get_with_retry(f"{AGENDA_CENTER}/Item/Edit/0?categoryID={category.id}&isDefault=False", "#txtTitle")
        d = self.b.d
        title = d.find_element(By.ID, "txtTitle")
        title.clear()
        title.send_keys(name)
        md = d.find_element(By.ID, "Item_MeetingDate")
        md.clear()
        md.send_keys(f"{meeting_date.month:02d}/{meeting_date.day:02d}/{meeting_date.year}")
        if item.meeting_time:
            st = d.find_element(By.ID, "txtStartTime")
            st.clear()
            st.send_keys(item.meeting_time)
        if item.description:
            d.execute_script("""
                const html = arguments[0];
                const ed = ((window.FroalaEditor && FroalaEditor.INSTANCES) || [])[0];
                if (ed) { ed.html.set(html); ed.events.trigger('contentChanged'); }
                const ta = document.querySelector('textarea[name$="FroalaDescription"]');
                if (ta) ta.value = html;
            """, item.description)
        if item.files:
            self._attach_item_files([resolve(f) for f in item.files])
        btn = d.find_element(By.ID, "btnSubmitItem")      # labelled "Save and Approve"
        start = d.current_url
        self.b.safe_click(btn)

        def saved():
            alert = self.b.take_alert(0.2)
            if alert is not None:
                self.b.dismiss_any_alert()
                raise SiteError(f"Site said: {alert}")
            return d.current_url != start
        self.b.wait_until(saved, timeout=UPLOAD_WAIT, poll=1.0)
        self.b.check_for_problems()
        new = [i for i in self.items(category) if i.id not in before and i.name == name]
        if not new:
            return None, "item saved but not found in the Items list"
        it = max(new, key=lambda i: i.id)
        if it.status != "approved":
            return it.id, f"item saved but its status is {it.status!r}, not approved"
        return it.id, "ok"

    def _attach_item_files(self, paths: list[Path]) -> None:
        d = self.b.d
        names = [p.name for p in paths]
        self.b.js_click(d.find_element(By.ID, "lnlUploadFilefileUploadControl"))
        self.b.wait_until(lambda: d.find_element(By.ID, "copyLinkDialog").is_displayed(), timeout=WAIT)
        d.switch_to.frame(d.find_element(By.ID, "copyLinkDialog"))
        try:
            # The pop-up's frame is pointed at the upload page when it opens;
            # wait for that page, not whatever the frame held before.
            self.b.wait_until(lambda: d.execute_script(
                "return location.href.indexOf('SelectFiles') >= 0 && document.readyState === 'complete' "
                "&& !!document.querySelector('input[type=file]');"), timeout=WAIT, poll=0.3)
            d.find_element(By.CSS_SELECTOR, "input[type=file]").send_keys(
                "\n".join(str(p.resolve()) for p in paths))
            # The pop-up is a Dropzone: each file must finish uploading
            # (dz-success + dz-complete) before Continue, or it is dropped.
            state_js = """return [...document.querySelectorAll('.dz-preview')].map(p => ({
                name: (p.querySelector('.dz-filename') || {textContent: ''}).textContent.trim(),
                done: p.classList.contains('dz-success') && p.classList.contains('dz-complete'),
                error: p.classList.contains('dz-error')
                       ? ((p.querySelector('.dz-error-message') || {textContent: 'error'}).textContent.trim()) : ''}));"""

            def uploaded() -> bool:
                st = d.execute_script(state_js)
                bad = [f"{x['name']}: {x['error']}" for x in st if x["error"]]
                if bad:
                    raise SiteError("Upload failed: " + "; ".join(bad))
                return len(st) >= len(paths) and all(x["done"] for x in st) \
                    and all(any(n == x["name"] for x in st) for n in names)
            try:
                self.b.wait_until(uploaded, timeout=UPLOAD_WAIT, poll=0.5)
            except TimeoutException:
                raise SiteError(f"Upload pop-up did not finish: {d.execute_script(state_js)}")
            cont = next(a for a in d.find_elements(By.TAG_NAME, "a") if a.text.strip().upper() == "CONTINUE")
            self.b.safe_click(cont)
        finally:
            d.switch_to.default_content()

        def attached() -> list[str]:
            return d.execute_script("return [...document.querySelectorAll("
                                    "'li.multipleFileUploads .spanFileName')].map(e => e.textContent.trim());")
        try:
            self.b.wait_until(lambda: all(n in attached() for n in names), timeout=60, poll=0.5)
        except TimeoutException:
            raise SiteError(f"Files did not attach to the item. Attached: {attached()}, expected: {names}")

    def _create_complex(self, category: Category, meeting: Meeting,
                        resolve: Callable[[str], Path]) -> tuple[Optional[int], str]:
        """Create each item, then a builder agenda holding them in order.
        Placing an item calls the builder's own add-item routine, the same
        one its drag-and-drop runs on drop."""
        item_ids: list[int] = []
        for it in meeting.items:
            iid, st = self.create_item(category, it, meeting.meeting_date, resolve)
            if iid is None or st != "ok":
                left = item_ids + ([iid] if iid else [])
                return None, f"item '{it.name}': {st}" + (f" (items left in the category: {left})" if left else "")
            item_ids.append(iid)

        before = {a.id for a in self.agendas(category)}
        self.b.get_with_retry(f"{AGENDA_CENTER}/Agenda/Edit/0?categoryID={category.id}", "#txtMeetingDate")
        d = self.b.d
        if d.find_element(By.ID, "chkSimpleAgenda").is_selected():
            raise SiteError("New agenda opened as 'Upload an agenda'; expected the builder.")
        self._set_meeting_date(site_date(meeting.meeting_date))
        desc = d.find_element(By.ID, "txtDesc")
        desc.clear()
        desc.send_keys(meeting.description)
        if meeting.video_link:
            d.find_element(By.ID, "txtVideoSource").send_keys(meeting.video_link)
        if meeting.additional_video_link:
            d.find_element(By.ID, "txtAdditionalVideoSource").send_keys(meeting.additional_video_link)

        def placed() -> list[int]:
            return d.execute_script(
                "return $('#divItems > .item .itemID').map((i, e) => parseInt(e.value, 10)).get();")
        for n, iid in enumerate(item_ids, 1):
            ok = d.execute_script("""
                const id = String(arguments[0]);
                const $p = $('#divItemList .item.onPanel').filter((i, e) => $(e).find('.itemID').val() === id);
                if ($p.length !== 1) return false;
                addSiblingItem($('#divItems > .item.level1:last'), $p);
                return true;""", iid)
            if not ok:
                return None, f"item {iid} is not offered in the agenda builder (items left in the category: {item_ids})"
            self.b.wait_until(lambda: placed() == item_ids[:n], timeout=WAIT, poll=0.3)

        start = d.current_url
        self.b.safe_click(d.find_element(By.ID, "btnSaveAgenda"))
        new_id = self._wait_for_agenda_saved(start, category, before, meeting)
        if new_id is None:
            return None, f"save did not complete (items left in the category: {item_ids})"
        return new_id, "ok"

    def _set_meeting_date(self, value: str) -> None:
        d = self.b.d
        main = d.find_element(By.ID, "txtMeetingDate")
        if main.is_displayed():
            main.clear()
            main.send_keys(value)
        # Make sure the field the form submits holds the value, then let the page react.
        d.execute_script("""
            const v = arguments[0];
            const main = document.getElementById('txtMeetingDate');
            if (main.value !== v) main.value = v;
            main.dispatchEvent(new Event('change', {bubbles: true}));
        """, value)
        got = d.find_element(By.ID, "txtMeetingDate").get_attribute("value")
        if parse_site_date(got) != parse_site_date(value):
            raise SiteError(f"Meeting date field shows {got!r}, expected {value!r}.")

    def _wait_for_agenda_saved(self, start_url: str, category: Category, before: set[int],
                               meeting: Meeting) -> Optional[int]:
        d = self.b.d

        def done():
            alert = self.b.take_alert(0.2)
            if alert is not None:
                self.b.dismiss_any_alert()
                raise SiteError(f"Site said: {alert}")
            return d.current_url != start_url and "/Agenda/Edit/0" not in d.current_url
        self.b.wait_until(done, timeout=UPLOAD_WAIT, poll=1.0)
        self.b.check_for_problems()
        m = re.search(r"agendaId=(\d+)", d.current_url, re.I)
        if m:
            return int(m.group(1))
        # Fall back to "the new agenda in this category with this date + description".
        new = [a for a in self.agendas(category)
               if a.id not in before and a.meeting_date == meeting.meeting_date
               and a.description.strip() == meeting.description.strip()]
        return max(a.id for a in new) if new else None

    def video_links(self, category: Category, agenda_id: int) -> tuple[str, str]:
        """(Link to Media, Additional Video Link) as saved on the agenda. Read only."""
        html = self.b.fetch_text(f"{AGENDA_CENTER}/Agenda/Edit/{agenda_id}?categoryID={category.id}")

        def value(el_id: str) -> str:
            m = re.search(r'<input[^>]*\bid="' + el_id + r'"[^>]*>', html)
            v = re.search(r'\bvalue="([^"]*)"', m.group(0)) if m else None
            return _unescape(v.group(1)).strip() if v else ""
        return value("txtVideoSource"), value("txtAdditionalVideoSource")

    def _answer_amended_dont_show(self, appear_timeout: float = 4.0) -> Optional[str]:
        """If the Amended Agenda pop-up appears, click Don't Show as Amended.
        Returns None when answered or not shown, else what went wrong."""
        d = self.b.d

        def modal():
            for m in d.find_elements(By.CSS_SELECTOR, ".modalAmendedAgenda"):
                if m.is_displayed():
                    return m
            return None
        try:
            m = self.b.wait_until(modal, timeout=appear_timeout, poll=0.3)
        except TimeoutException:
            return None
        for a in m.find_elements(By.CSS_SELECTOR, ".buttons a"):
            onclick = (a.get_attribute("onclick") or "").replace(" ", "")
            if "amendmentModalCallback(false)" in onclick and "don't show" in a.text.lower().replace("’", "'"):
                self.b.js_click(a)
                self.r.info("Amended Agenda pop-up answered: Don't Show as Amended.")
                return None
        return "the Amended Agenda pop-up had no Don't Show as Amended button; nothing saved"

    def edit_and_save(self, category: Category, agenda_id: int, video: Optional[str] = None,
                      additional: Optional[str] = None) -> str:
        """Open an agenda's edit page, set Link to Media / Additional Video Link
        when given, and Save. Saving is also what makes Central build the
        public HTML page of an item-based (builder) agenda, which an import
        doesn't do. Notify Me, if it appears, is answered Don't Send. The
        agenda keeps its status (a draft stays a draft)."""
        self.b.get_with_retry(f"{AGENDA_CENTER}/Agenda/Edit/{agenda_id}?categoryID={category.id}", "#txtMeetingDate")
        d = self.b.d
        for el_id, value in (("txtVideoSource", video), ("txtAdditionalVideoSource", additional)):
            if value is None:
                continue
            els = d.find_elements(By.ID, el_id)
            if not els:
                return f"the edit page has no {el_id} field"
            els[0].clear()
            if value:
                els[0].send_keys(value)
        # Imports stamp Posted date with the import time, and the edit page
        # won't save a Posted date after the Meeting date (seen live
        # 2026-11-17). Move it back to the meeting date.
        posted, meeting = d.find_elements(By.ID, "Agenda_PostedDate"), d.find_elements(By.ID, "txtMeetingDate")
        if posted and meeting:
            pv, mv = posted[0].get_attribute("value") or "", meeting[0].get_attribute("value") or ""
            pd, md = _mdy(pv), _mdy(mv)
            if pd and md and pd > md:
                posted[0].clear()
                posted[0].send_keys(mv)
                if (posted[0].get_attribute("value") or "") != mv:
                    return f"couldn't move Posted date {pv} back to the meeting date {mv}; nothing saved"
                self.r.info(f"Agenda {agenda_id}: Posted date {pv} is after the meeting; set to {mv}.")
        buttons = d.find_elements(By.ID, "btnSaveAgenda")
        if not buttons:
            return "the edit page has no Save button"
        start = d.current_url
        btn = buttons[0]
        self.b.safe_click(btn)
        # Saving a published agenda first asks "show as amended?" (seen live
        # 2026-11-17). A migration fix-up isn't an amendment: Don't Show.
        problem = self._answer_amended_dont_show(appear_timeout=4)
        if problem:
            return problem

        # A plain Save never opens Notify Me (only Publish/Send do), so it's
        # answered from inside the wait rather than waited for: waiting for it
        # used to swallow the site's validation alert and call the save ok.
        rss_answered: list = []

        def done():
            try:
                alert = self.b.take_alert(0.2)
            except UnexpectedAlertPresentException as e:
                alert = e.alert_text or "an alert"
            if alert is not None:
                self.b.dismiss_any_alert()
                if "rss feed" in alert.lower():
                    # "Include this in the RSS feed? OK/Cancel": Cancel keeps
                    # old meetings out of the feed (like Notify Me: Don't
                    # Send) and the save carries on.
                    rss_answered.append(1)
                    return False
                raise SiteError(f"Site said: {alert}")
            try:
                if d.current_url != start:
                    return True
                if self.b.notify_modal_open():
                    self.b.answer_notify_dont_send(appear_timeout=1)
                    return False
                btn.is_enabled()
                return False
            except StaleElementReferenceException:
                return True           # same address, but the page was reloaded
            except UnexpectedAlertPresentException as e:
                self.b.dismiss_any_alert()
                raise SiteError(f"Site said: {e.alert_text or 'an alert'}")
        try:
            self.b.wait_until(done, timeout=UPLOAD_WAIT, poll=1.0)
        except TimeoutException:
            return "the page didn't reload after Save; it may not have saved"
        if rss_answered:
            self.r.info("RSS feed question answered: Cancel (not added to the feed).")
        self.b.check_for_problems()
        if video is not None or additional is not None:
            got = self.video_links(category, agenda_id)
            want = (video if video is not None else got[0], additional if additional is not None else got[1])
            if got != want:
                return f"saved, but the video links read back as {got}"
        return "ok"

    def add_minutes(self, category: Category, agenda_id: int, minutes_path: Path, publish: bool = True) -> str:
        """ACTIONS > Add Minutes on a PUBLISHED agenda (the site only offers it
        then). Minutes have their own status: publish=True uses Save and
        Publish (Notify Me answered Don't Send), otherwise they stay Draft."""
        self.b.get_with_retry(f"{AGENDA_CENTER}/Minutes/Edit/0?agendaID={agenda_id}", "#simpleMinutesFile")
        d = self.b.d
        chk = d.find_element(By.ID, "chkSimpleMinutes")
        if not chk.is_selected():
            self.b.safe_click(chk)
            self.b.answer_confirm(["minutes that you have saved"])
        self.b.wait_until(lambda: d.find_element(By.ID, "simpleMinutesFile").is_displayed())
        d.find_element(By.ID, "simpleMinutesFile").send_keys(str(minutes_path.resolve()))
        wanted = "saveMinutes('publish'" if publish else "saveMinutes('save'"
        btn = next((a for a in d.find_elements(By.CSS_SELECTOR, "a.button")
                    if wanted in (a.get_attribute("onclick") or "")), None)
        if btn is None:
            return "could not find the minutes save button"
        start_url = d.current_url
        self.b.safe_click(btn)
        if publish:
            self.b.answer_notify_dont_send()

        def done():
            alert = self.b.take_alert(0.2)
            if alert is not None:
                self.b.dismiss_any_alert()
                raise SiteError(f"Site said: {alert}")
            return d.current_url != start_url
        self.b.wait_until(done, timeout=UPLOAD_WAIT, poll=1.0)
        self.b.check_for_problems()
        agenda = next((a for a in self.agendas(category) if a.id == agenda_id), None)
        if agenda is None or not agenda.has_minutes:
            return "minutes not showing on the agenda"
        _, status = self.minutes_info(agenda_id, agenda.minutes_id)
        want = "Published" if publish else "Draft"
        return "ok" if status.lower() == want.lower() else f"minutes saved but status is {status!r}, expected {want}"


def _unescape(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
            .replace("&lt;", "<").replace("&gt;", ">"))


def _mdy(text: str):
    """MM/DD/YYYY from an edit page date box, or None."""
    import datetime as _dt
    try:
        return _dt.datetime.strptime(text.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None
