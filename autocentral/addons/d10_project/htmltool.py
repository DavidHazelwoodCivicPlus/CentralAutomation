"""Run the team's D10-to-Central Migration Tool (.html) hidden, the way a
person clicks through it, and keep its outputs: the site map (from the old
site's home page and /sitemap, fetched here), the Resource Directory file,
the staff and department files and the rest of what its buttons make.
"""

from __future__ import annotations

import base64
import time
import urllib.request
from pathlib import Path
from typing import Callable

CAPTURE = r"""
window.__caps = [];
URL.revokeObjectURL = function () {};
const toB64 = buf => { let s = ''; const b = new Uint8Array(buf);
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000)); return btoa(s); };
HTMLAnchorElement.prototype.click = function () {
  const name = this.download, href = this.href || '';
  if (name && (href.startsWith('blob:') || href.startsWith('data:')))
    window.__caps.push(fetch(href).then(r => r.arrayBuffer()).then(b => ({ name, b64: toB64(b) })));
};
window.alert = m => {}; window.confirm = m => true;
"""
GENERATE = ["smGenerateBtn", "dcGenerateBtn", "generateBtn", "queueForPagesBtn", "workflowItemGenerateBtn"]
ZONES = {"Eastern": "America/New_York", "Central": "America/Chicago", "Mountain": "America/Denver",
         "Arizona": "America/Phoenix", "Pacific": "America/Los_Angeles", "Alaska": "America/Anchorage",
         "Hawaii": "Pacific/Honolulu"}


def fetch_page(url: str, out: Path) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CentralAutomation"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out.write_bytes(r.read())
    return out


def fetch_with(driver, old_host: str, src: Path) -> dict:
    """The old site's home page and /sitemap through a real (visible) Chrome,
    in a tab of its own: D10 sites send hidden browsers a bot check instead."""
    home = driver.current_window_handle
    out = {}
    driver.switch_to.new_window("tab")
    try:
        for name, path in (("home.html", "/"), ("sitemap.html", "/sitemap")):
            try:
                driver.get(f"https://{old_host}{path}")
                time.sleep(3)
                html = driver.page_source
                if len(html) > 2000:
                    (src / name).write_text(html, encoding="utf-8")
                    out[name] = src / name
            except Exception:
                pass
    finally:
        driver.close()
        driver.switch_to.window(home)
    return out


def run(tool: Path, exports: Path, old_host: str, zone: str, out_dir: Path,
        log: Callable[[str], None] = print, sources: dict | None = None) -> dict[str, Path]:
    """sources: {"home.html": path, "sitemap.html": path} already fetched."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / "source"
    src.mkdir(exist_ok=True)
    exports_files = sorted(p for p in Path(exports).glob("*.json") if not p.name.startswith(("project", "uploaded")))
    o = webdriver.ChromeOptions()
    o.add_argument("--headless=new")
    o.add_argument("--allow-file-access-from-files")
    d = webdriver.Chrome(options=o)
    d.set_script_timeout(900)
    got: dict[str, Path] = {}
    try:
        # The old site's home page and /sitemap. Through Chrome: D10 sites turn
        # away plain downloads (HTTP 403).
        home, smap = (sources or {}).get("home.html"), (sources or {}).get("sitemap.html")
        for name, path in (() if home else (("home.html", "/"), ("sitemap.html", "/sitemap"))):
            try:
                d.get(f"https://{old_host}{path}")
                time.sleep(2)
                html = d.page_source
                if len(html) > 2000 and "403 Forbidden" not in html[:2000]:
                    (src / name).write_text(html, encoding="utf-8")
                    if name == "home.html":
                        home = src / name
                    else:
                        smap = src / name
            except Exception as e:
                log(f"  couldn't read {path} on the old site ({e})")
        if home is None:
            log("  the old site's home page couldn't be read: no site map from the migration tool")
        d.get(Path(tool).resolve().as_uri())
        d.find_element(By.ID, "fileInput").send_keys("\n".join(str(p.resolve()) for p in exports_files))
        t0 = time.time()
        while d.execute_script("return typeof contentStore !== 'undefined' ? Object.keys(contentStore).length : 0;") \
                < len(exports_files) and time.time() - t0 < 300:
            time.sleep(0.5)
        d.execute_script("const s = document.getElementById('siteTimezoneSelect'); s.value = arguments[0];"
                         "s.dispatchEvent(new Event('change', {bubbles: true}));", ZONES.get(zone, zone))
        d.execute_script(CAPTURE)
        for inp, f in (("smHomeFileInput", home), ("smDrupalFileInput", smap)):
            if f is None:
                continue
            el = d.find_element(By.ID, inp)
            d.execute_script("arguments[0].style.display='block'; arguments[0].classList.remove('hidden');", el)
            el.send_keys(str(f.resolve()))
            time.sleep(1.5)
        for g in GENERATE:
            d.execute_script("const b = document.getElementById(arguments[0]); if (b) { b.disabled = false; try { b.click(); } catch (e) {} }", g)
            time.sleep(3)
        btns = d.execute_script("return [...document.querySelectorAll('button[id]')].map(b => b.id).filter(id => /download/i.test(id))")
        for bid in btns:
            d.execute_script("const b = document.getElementById(arguments[0]); b.disabled = false; try { b.click(); } catch (e) {}", bid)
            time.sleep(1.5)
        files = d.execute_async_script("const done = arguments[0]; Promise.all(window.__caps).then(done).catch(e => done([]));")
    finally:
        d.quit()
    for f in files:
        p = out_dir / f["name"]
        p.write_bytes(base64.b64decode(f["b64"]))
        got[f["name"]] = p
    log(f"  migration tool made {len(got)} file(s): " + ", ".join(sorted(got)[:12]))
    return got
