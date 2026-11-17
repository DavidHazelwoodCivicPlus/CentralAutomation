"""Selenium session for one CivicPlus Central site (all modules).

Rules carried over from the Agenda Center and Staff Directory tools:
  * The user always logs in by hand (2FA); the tool waits, then takes over.
  * Reading never changes anything: background fetch() for the newer admin
    pages, hidden same-site iframes for the classic .aspx pages. Reads never
    open edit pages and never press Save.
  * Changes (later phases) go through the real rendered controls, then the
    site is read again to confirm.
  * NEVER notify subscribers. safe_click() refuses anything that looks like
    a Send control, and the Notify Me pop-up is only ever answered with
    "Don't Send" (or Cancel if that button can't be found).
"""

from __future__ import annotations

import re
import time
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import (
    NoAlertPresentException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .reporting import CancelToken, Reporter

WAIT = 15
ERROR_PAGE_XPATH = "//*[contains(text(), 'An error has occurred')]"
SESSION_TIMEOUT_MODAL_ID = "cp-session-timeout-modal"
SESSION_REFRESH_SELECTOR = '[data-session-timeout-action="refresh"]'

NOTIFY_IFRAME_ID = "NotifyMeDialog"
DONT_SEND_LABEL = "Don't Send"


class ForbiddenAction(Exception):
    """The tool was about to touch a control that could notify subscribers."""


class SiteError(Exception):
    pass


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def looks_like_send_control(text: str, value: str, element_id: str, onclick: str) -> bool:
    """True for anything that could send email/SMS to subscribers.

    "Don't Send" is the one Send-ish control we are allowed to click, and only
    from inside the Notify Me pop-up (see answer_notify_dont_send)."""
    label = _norm(text) or _norm(value)
    if label in ("send", "save and send") or label.endswith(" and send"):
        return True
    if element_id in ("btnSendAgenda",):
        return True
    oc = (onclick or "").replace(" ", "")
    if "'send'" in oc or '"send"' in oc:
        return True
    if re.search(r"sendEmailBE\([^,]*,[^,]*,[^,]*,true", oc):
        return True
    return False


# JavaScript for frames(): loads jobs in hidden iframes, `parallel` at a time.
_FRAMES_JS = r"""
const [jobs, parallel, settle, pageTimeout] = arguments;
const out = new Array(jobs.length);
let next = 0;
function guard(w) {
    try { w.alert = () => {}; w.confirm = () => false; w.prompt = () => null; w.open = () => null; } catch (e) {}
}
function runJob(job) {
    return new Promise(resolve => {
        const f = document.createElement('iframe');
        f.style.cssText = 'position:absolute;width:1200px;height:900px;left:-5000px;top:0;visibility:hidden';
        const steps = job.steps || [];
        const pages = [];
        let stage = 0, timer = null, finished = false;
        const finish = (ok, error) => {
            if (finished) return;
            finished = true;
            clearTimeout(timer);
            let url = '', snapshot = '';
            try { url = f.contentWindow.location.href; } catch (e) {}
            // on failure keep what the frame shows, for the report
            if (!ok) { try { snapshot = f.contentDocument.documentElement.outerHTML; } catch (e) {} }
            f.remove();
            resolve({ok: ok, url: url, pages: pages, error: error || '', snapshot: snapshot});
        };
        const arm = () => {
            clearTimeout(timer);
            timer = setTimeout(() => finish(false, 'timed out at step ' + stage), pageTimeout * 1000);
        };
        // A step either starts a page load (onload continues), or returns a
        // Promise for an in-place update (such as an ASP.NET partial postback)
        // and the next step runs when it resolves.
        // Only the latest load counts: a page that loads twice (a redirect)
        // must not run two steps. The document is read again after `settle`,
        // and a frame caught between two pages (no document yet) is retried.
        let waiting = false, loadSeq = 0;
        const onPage = (tries) => {
            const mine = ++loadSeq;
            tries = tries || 0;
            setTimeout(() => {
                if (finished || mine !== loadSeq) return;
                let w, doc;
                try { w = f.contentWindow; doc = f.contentDocument; }
                catch (e) { finish(false, 'frame not readable: ' + e); return; }
                if (!doc || !doc.documentElement || !doc.body) {
                    if (tries < 20) { loadSeq--; onPage(tries + 1); } else finish(false, 'frame stayed empty');
                    return;
                }
                guard(w);
                try {
                    if (job.capture === 'each' || stage === steps.length) pages.push(doc.documentElement.outerHTML);
                    if (stage === steps.length) { finish(true); return; }
                    const js = steps[stage++];
                    arm();
                    const res = (new Function('w', 'doc', 'cur', js))(w, doc, () => f.contentDocument);
                    if (res === false) finish(false, 'step ' + stage + ' found nothing to do');
                    else if (res && typeof res.then === 'function') {
                        waiting = true;
                        res.then(() => { waiting = false; onPage(); },
                                 e => finish(false, 'step ' + stage + ' failed: ' + e));
                    }
                } catch (e) { finish(false, 'step ' + stage + ' failed: ' + e); }
            }, (tries ? 0.5 : settle) * 1000);
        };
        f.onload = () => { if (!waiting) onPage(0); };
        arm();
        f.src = job.src;
        document.body.appendChild(f);
    });
}
async function worker() {
    while (next < jobs.length && !__state.cancel) { const i = next++; out[i] = await runJob(jobs[i]); __state.progress++; }
}
Promise.all(Array.from({length: Math.min(parallel, jobs.length)}, worker)).then(() => done(out));
"""

_FETCH_MANY_JS = r"""
const [urls, parallel] = arguments;
const out = new Array(urls.length);
let next = 0;
async function worker() {
    while (next < urls.length && !__state.cancel) {
        const i = next++;
        try {
            const r = await fetch(urls[i], {credentials: 'same-origin', cache: 'no-store'});
            out[i] = {ok: r.ok, status: r.status, url: r.url, text: await r.text()};
        } catch (e) { out[i] = {ok: false, status: 0, url: urls[i], text: String(e)}; }
    }
}
Promise.all(Array.from({length: Math.min(parallel, urls.length)}, worker)).then(() => done(out));
"""


class Browser:
    def __init__(self, site_url: str, reporter: Reporter, cancel: Optional[CancelToken] = None,
                 headless: bool = False) -> None:
        self.site_url = site_url.strip().rstrip("/")
        if not re.match(r"^https?://", self.site_url):
            self.site_url = "https://" + self.site_url
        self.host = urlparse(self.site_url).netloc.lower()
        self.r = reporter
        self.cancel = cancel or CancelToken()
        self.headless = headless
        self.driver: Optional[webdriver.Chrome] = None

    # ------------------------------------------------------------------ setup
    def start(self, debugger_address: Optional[str] = None) -> None:
        """Start Chrome, or attach to one already running with
        --remote-debugging-port (keeps a login alive across test runs)."""
        options = webdriver.ChromeOptions()
        if debugger_address:
            options.add_experimental_option("debuggerAddress", debugger_address)
        else:
            if self.headless:
                options.add_argument("--headless=new")
            options.add_argument("--start-maximized")
            # The reads run on timers inside the page. Chrome slows those to
            # one every few seconds (or a minute) when the window is minimized
            # or behind others; these keep scans at full speed regardless.
            for flag in ("--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                         "--disable-renderer-backgrounding"):
                options.add_argument(flag)
        self.driver = webdriver.Chrome(options=options)

    def quit(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            finally:
                self.driver = None

    @property
    def d(self) -> webdriver.Chrome:
        if self.driver is None:
            raise RuntimeError("Browser not started")
        return self.driver

    def url(self, path: str) -> str:
        return self.site_url + ("" if path.startswith("/") else "/") + path

    def get(self, path: str) -> None:
        self.cancel.check()
        self.d.get(self.url(path))
        self.check_for_problems()

    # ------------------------------------------------------------------ login
    def open_for_login(self) -> None:
        self.d.get(self.url("/Admin"))

    def is_logged_in(self) -> bool:
        """On the site's own host, inside /Admin, page loaded, no password box."""
        try:
            cur = urlparse(self.d.current_url)
            path = cur.path.lower()
            if cur.netloc.lower() != self.host or not path.startswith("/admin") or "login" in path:
                return False
            if self.d.execute_script("return document.readyState") != "complete":
                return False
            return not self.d.find_elements(By.CSS_SELECTOR, "input[type=password]")
        except WebDriverException:
            return False

    def wait_for_login(self, timeout: Optional[float] = None,
                       confirmed: Optional[Callable[[], bool]] = None) -> bool:
        """Poll until the user has finished logging in (including 2FA).
        Once the browser is back on the site but outside /Admin, it steers
        to /Admin. `confirmed` lets a front end say "I'm done" early."""
        start = time.time()
        last_nudge = 0.0
        while True:
            self.cancel.check()
            if self.is_logged_in():
                return True
            try:
                cur = urlparse(self.d.current_url)
                on_site = cur.netloc.lower() == self.host and "login" not in cur.path.lower()
            except WebDriverException:
                on_site = False
            if (on_site or (confirmed and confirmed())) and time.time() - last_nudge > 5:
                last_nudge = time.time()
                try:
                    self.d.get(self.url("/Admin"))
                except WebDriverException:
                    pass
                if self.is_logged_in():
                    return True
            if timeout is not None and time.time() - start > timeout:
                return False
            time.sleep(1.0)

    # ------------------------------------------------------- page health
    def handle_session_timeout(self) -> bool:
        try:
            modal = self.d.find_element(By.ID, SESSION_TIMEOUT_MODAL_ID)
            if not modal.is_displayed():
                return False
            btn = modal.find_element(By.CSS_SELECTOR, SESSION_REFRESH_SELECTOR)
            self.js_click(btn)
            self.r.info("Session-timeout prompt appeared; clicked Refresh Session.")
            time.sleep(1.0)
            return True
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            return False

    def on_error_page(self) -> bool:
        try:
            return len(self.d.find_elements(By.XPATH, ERROR_PAGE_XPATH)) > 0
        except WebDriverException:
            return False

    def on_login_page(self) -> bool:
        try:
            cur = urlparse(self.d.current_url)
        except WebDriverException:
            return False
        return cur.netloc.lower() != self.host or "login" in cur.path.lower()

    def check_for_problems(self) -> None:
        self.handle_session_timeout()
        if self.on_login_page():
            raise SiteError("The site logged us out (on a sign-in page). Log in again and re-run.")
        if self.on_error_page():
            raise SiteError("The site showed its 'An error has occurred' page.")

    def sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.cancel.check()
            self.handle_session_timeout()
            time.sleep(min(1.0, max(0.0, end - time.time())))

    # ------------------------------------------------------------- elements
    def wait_css(self, css: str, timeout: float = WAIT) -> WebElement:
        return WebDriverWait(self.d, timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR, css)))

    def wait_until(self, fn: Callable[[], object], timeout: float = WAIT, poll: float = 0.4) -> object:
        end = time.time() + timeout
        last_exc: Optional[Exception] = None
        while time.time() < end:
            self.cancel.check()
            try:
                v = fn()
                if v:
                    return v
            except (StaleElementReferenceException, NoSuchElementException, WebDriverException) as e:
                last_exc = e
            time.sleep(poll)
        raise TimeoutException(f"Timed out after {timeout}s" + (f" ({last_exc})" if last_exc else ""))

    def js_click(self, el: WebElement) -> None:
        self.d.execute_script("arguments[0].click();", el)

    def safe_click(self, el: WebElement, allow_dont_send: bool = False) -> None:
        """Click a real control, refusing anything that could notify subscribers."""
        self.cancel.check()
        text = el.text or ""
        value = el.get_attribute("value") or ""
        eid = el.get_attribute("id") or ""
        onclick = el.get_attribute("onclick") or ""
        label = _norm(text) or _norm(value)
        if label == _norm(DONT_SEND_LABEL):
            if not allow_dont_send:
                raise ForbiddenAction("Refusing to click a Send/Don't Send control outside the Notify Me handler.")
        elif looks_like_send_control(text, value, eid, onclick):
            raise ForbiddenAction(f"Refusing to click a control that could notify subscribers: {label or eid!r}")
        self.d.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
        self.js_click(el)

    # ------------------------------------------------------------- dialogs
    def take_alert(self, timeout: float = 3.0) -> Optional[str]:
        try:
            WebDriverWait(self.d, timeout).until(EC.alert_is_present())
            return self.d.switch_to.alert.text
        except TimeoutException:
            return None

    def answer_confirm(self, expected: Iterable[str], timeout: float = 5.0) -> str:
        """Accept a confirm() whose text contains one of the expected phrases.
        Anything else is dismissed and reported as an error."""
        text = self.take_alert(timeout)
        if text is None:
            raise SiteError(f"Expected a confirmation dialog ({' / '.join(expected)}) but none appeared.")
        alert = self.d.switch_to.alert
        if any(_norm(e) in _norm(text) for e in expected):
            alert.accept()
            return text
        alert.dismiss()
        raise SiteError(f"Unexpected dialog, dismissed it: {text!r}")

    def dismiss_any_alert(self) -> Optional[str]:
        try:
            alert = self.d.switch_to.alert
            text = alert.text
            alert.dismiss()
            return text
        except NoAlertPresentException:
            return None

    # ------------------------------------------------------- notify me
    def notify_modal_open(self) -> bool:
        try:
            frames = self.d.find_elements(By.ID, NOTIFY_IFRAME_ID)
            return any(f.is_displayed() and "NotifyEmails" in (f.get_attribute("src") or "") for f in frames)
        except WebDriverException:
            return False

    def answer_notify_dont_send(self, appear_timeout: float = 8.0,
                                precheck: Optional[Callable[[], bool]] = None) -> bool:
        """If the Notify Me pop-up appears, answer it with Don't Send. If the
        Don't Send button can't be found the pop-up is cancelled and
        ForbiddenAction is raised: we never guess at a button in there."""
        try:
            self.wait_until(self.notify_modal_open, timeout=appear_timeout)
        except TimeoutException:
            return False
        precheck_ok = precheck() if precheck else True
        self.d.switch_to.frame(self.d.find_element(By.ID, NOTIFY_IFRAME_ID))
        try:
            if not precheck_ok:
                raise ForbiddenAction("Selection changed while the Notify Me pop-up was open")

            def find_dont_send():
                for b in self.d.find_elements(By.CSS_SELECTOR, "input[type=button], button"):
                    if _norm(b.get_attribute("value") or b.text) == _norm(DONT_SEND_LABEL):
                        return b
                return None
            btn = self.wait_until(find_dont_send, timeout=10)
            onclick = (btn.get_attribute("onclick") or "").replace(" ", "")
            if re.search(r"sendEmailBE\([^,]*,[^,]*,[^,]*,true", onclick):
                raise ForbiddenAction("The 'Don't Send' button is wired to send. Stopping.")
            self.safe_click(btn, allow_dont_send=True)
        except (TimeoutException, ForbiddenAction) as e:
            for b in self.d.find_elements(By.CSS_SELECTOR, "input[type=button], button"):
                if _norm(b.get_attribute("value") or b.text) == "cancel":
                    self.js_click(b)
                    break
            raise ForbiddenAction(f"Notify Me pop-up cancelled without publishing: {e or 'no safe Don’t Send button'}")
        finally:
            self.d.switch_to.default_content()
        self.r.info("Notify Me pop-up answered: Don't Send.")
        return True

    # ------------------------------------------------------- read-only reads
    def _check_host(self, url: str) -> None:
        final = urlparse(url or "")
        if final.netloc and final.netloc.lower() != self.host:
            raise SiteError("A page was redirected off-site (logged out?). Log in again and re-run.")

    def _run_background(self, body: str, *args, timeout: float = 600.0):
        """Start `body` in the page without waiting, then check on it twice a
        second. Long reads don't hit Selenium's 120 s command limit, and Stop
        works mid-read: the page stops starting new pages. `body` reads its
        inputs from `arguments`, calls done(value) when finished, and checks
        __state.cancel between pages."""
        starter = ("const __t = 'ac' + Date.now() + Math.random().toString(36).slice(2);"
                   "window.__ac = window.__ac || {};"
                   "const __state = window.__ac[__t] = {done: false, cancel: false, progress: 0};"
                   "const done = v => { __state.value = v; __state.done = true; };"
                   + body + "\nreturn __t;")
        token = self.d.execute_script(starter, *args)
        poll = ("const s = (window.__ac || {})[arguments[0]];"
                "if (!s) return {gone: true};"
                "if (arguments[1]) s.cancel = true;"
                "if (!s.done) return {done: false};"
                "delete window.__ac[arguments[0]];"
                "return {done: true, value: s.value};")
        end = time.time() + timeout
        while True:
            time.sleep(0.5)
            stop = self.cancel.cancelled
            state = self.d.execute_script(poll, token, stop)
            if state.get("gone"):
                raise SiteError("The browser page changed while reading (did the tab navigate?). Run the scan again.")
            if state.get("done"):
                self.cancel.check()
                return state.get("value") or []
            if stop:
                # let the pages in flight finish, then report the stop
                continue
            if time.time() > end:
                self.d.execute_script(poll, token, True)
                raise SiteError(f"Reading pages took longer than {timeout:.0f}s; stopped.")

    def fetch_many(self, paths: list[str], parallel: int = 6) -> list[dict]:
        """Fetch several pages at once with the logged-in session. Returns
        [{path, ok, status, url, text}] in order. Read-only use only. Classic
        .aspx pages come back as a redirect to Classic.aspx; use frames()."""
        if not paths:
            return []
        self.cancel.check()
        res = self._run_background(_FETCH_MANY_JS, [self.url(p) for p in paths], parallel,
                                   timeout=60 + 30 * len(paths))
        for path, r in zip(paths, res):
            r["path"] = path
            self._check_host(r.get("url"))
        return res

    def fetch_bytes(self, path: str) -> tuple[bytes, str]:
        """Download a file (such as a module's own Export) with the logged-in
        session, without saving it anywhere. Returns (content, content type)."""
        import base64
        self.cancel.check()
        body = r"""
            const [url] = arguments;
            fetch(url, {credentials: 'same-origin', cache: 'no-store'}).then(async r => {
                const buf = new Uint8Array(await r.arrayBuffer());
                let bin = '';
                for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
                done({ok: r.ok, status: r.status, url: r.url, type: r.headers.get('content-type') || '', b64: btoa(bin)});
            }).catch(e => done({ok: false, status: 0, url, type: '', b64: '', error: String(e)}));
        """
        r = self._run_background(body, self.url(path), timeout=300)
        self._check_host(r.get("url"))
        if not r.get("ok"):
            raise SiteError(f"Download {path} failed: HTTP {r.get('status')} {r.get('error', '')}")
        return base64.b64decode(r["b64"]), r.get("type", "")

    def fetch_text(self, path: str) -> str:
        r = self.fetch_many([path])[0]
        if not r.get("ok"):
            raise SiteError(f"Fetch {path} failed: HTTP {r.get('status')}")
        return r["text"]

    def frames(self, jobs: list[dict], parallel: int = 4, settle: float = 1.0,
               page_timeout: float = 60.0) -> list[dict]:
        """Load pages in hidden iframes, several at once, and return their HTML.

        Classic .aspx admin pages only render as a real page visit, so plain
        fetch() can't read them; a hidden same-site iframe can. Each job is
        {"src": path, "steps": [js, ...], "capture": "last" | "each"}. After
        the page loads (plus `settle` seconds), each step's JavaScript runs
        with `w` (the frame's window) and `doc` (its document) and must start
        a navigation, such as picking "View List" or running a search. Return
        false from a step when there is nothing to do. The HTML is captured
        after the last load, or after every load with capture "each".

        Read-only safety: inside the frame, alert/confirm/prompt are replaced
        so any confirmation is answered Cancel, and window.open does nothing.

        Returns [{"ok", "url", "pages": [html, ...], "error"}] in job order."""
        if not jobs:
            return []
        self.cancel.check()
        longest = max(len(j.get("steps") or []) + 1 for j in jobs)
        rounds = -(-len(jobs) // max(1, parallel))
        res = self._run_background(_FRAMES_JS, [{**j, "src": self.url(j["src"])} for j in jobs],
                                   parallel, settle, page_timeout,
                                   timeout=30 + rounds * longest * (page_timeout + settle + 5))
        for r in res:
            self._check_host(r.get("url"))
        return res
