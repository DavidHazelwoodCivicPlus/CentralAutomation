// In-page action library for every module except Agenda Center (which has its
// own code in agendacenter/site.py). Loaded into the logged-in admin page by
// actions/engine.py and called with execute_async_script.
//
// The way the WebCentral Relay extension does it: requests sent from inside
// the logged-in page with fetch(), IDs in bulk where the site takes them.
// Classic .aspx pages bounce a plain GET to Classic.aspx, so their forms are
// read from a hidden frame; their POSTs work as plain fetch().
//
// Safety: nothing here ever sends a notification. Every form posted has its
// send fields (ysnSend, ysnSendAtStart, ysnSendBeforeDays, ysnNotifyMe) set
// to "don't send", which is what the Notify Me pop-up's Don't Send button
// posts. Categories are only deleted after the site's own checks pass.
//
// Every module object has:
//   cats()                    -> [{id, name, published}]
//   items(cid)                -> [{id, name, published}]   (every page)
//   catAct(action, ids)       -> {ok: [ids], failed: {id: why}}   action: publish|unpublish|delete
//   itemAct(action, cid, ids) -> {ok: [ids], failed: {id: why}}
(function () {
  const VERSION = '__VERSION__';   // replaced with a hash of this file when loaded
  if (window.__acRelay && window.__acRelay.version === VERSION) return;

  const clean = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const parse = t => new DOMParser().parseFromString(t, 'text/html');
  const XHR = {'X-Requested-With': 'XMLHttpRequest'};
  // Exactly what the Notify Me pop-up's Don't Send posts (publishItemBE(false, false, false)).
  const NO_SEND = {ysnSend: 'false', ysnSendAtStart: 'false', ysnSendBeforeDays: 'false', intSendBeforeDays: '-1',
                   txtSubject: '', txtComments: '', txtSMSComments: ''};

  async function http(url, opts) {
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), 180000);
    try {
      const r = await fetch(url, Object.assign({credentials: 'same-origin', cache: 'no-store'}, opts || {}, {signal: c.signal}));
      const text = await r.text();
      if (/\/(Login|SignIn)|cpauthentication/i.test(r.url)) throw new Error('logged out');
      if (r.ok && /An error has occurred/i.test(text) && !/<form/i.test(text.slice(0, 20000)))
        return {ok: false, status: r.status, url: r.url, text};
      return {ok: r.ok, status: r.status, url: r.url, text};
    } finally { clearTimeout(t); }
  }
  const post = (url, body, headers) => http(url, {method: 'POST', body, headers});
  const form = obj => ({body: new URLSearchParams(obj).toString(),
                        headers: Object.assign({'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}, XHR)});
  const isClassic = u => /\/Admin\/Classic\.aspx/i.test(u || '');

  // A classic page as a real visit in a hidden frame; returns its document's HTML.
  function frameHtml(path, timeoutMs) {
    return new Promise((resolve, reject) => {
      const f = document.createElement('iframe');
      f.style.cssText = 'position:fixed;left:-9999px;top:0;width:1200px;height:900px;visibility:hidden';
      const timer = setTimeout(() => { f.remove(); reject(new Error('timed out loading ' + path)); }, timeoutMs || 60000);
      f.onload = () => {
        clearTimeout(timer);
        try { const w = f.contentWindow; w.alert = () => {}; w.confirm = () => false; } catch (e) {}
        let html = '';
        try { html = f.contentDocument.documentElement.outerHTML; } catch (e) { f.remove(); return reject(e); }
        f.remove();
        if (/\/(Login|SignIn)/i.test(String(f.src)) ) return reject(new Error('logged out'));
        resolve(html);
      };
      f.src = path;
      document.body.append(f);
    });
  }
  async function getDoc(path) {
    const x = await http(path);
    if (x.ok && !isClassic(x.url)) return parse(x.text);
    if (x.status === 404) throw new Error('module not on this site (404)');
    return parse(await frameHtml(path));
  }
  // A classic module's page form, kept for a few minutes (its hidden fields don't change).
  const formCache = {};
  async function pageForm(path, sel) {
    const k = path + '|' + sel;
    const c = formCache[k];
    if (c && Date.now() - c.at < 180000) return c.entries;
    const doc = await getDoc(path);
    const f = doc.querySelector(sel);
    if (!f) throw new Error('the page did not load as expected (no ' + sel + ')');
    const entries = [...new FormData(f).entries()];
    formCache[k] = {entries, at: Date.now()};
    return entries;
  }
  function fdFrom(entries, set) {
    const fd = new FormData();
    entries.forEach(([k, v]) => fd.append(k, v));
    Object.entries(set || {}).forEach(([k, v]) => fd.set(k, String(v)));
    return fd;
  }
  // The checkbox bulk form in an HTML page, with every cbox* field dropped.
  function bulkForm(doc, sel) {
    const f = sel ? doc.querySelector(sel) : [...doc.forms].find(x => x.querySelector('input[name=strBulkAction]'));
    if (!f) return null;
    const fd = new FormData(f);
    [...new Set([...fd.keys()])].filter(k => /^cbox/.test(k)).forEach(k => fd.delete(k));
    Object.entries(NO_SEND).forEach(([k, v]) => { if (fd.has(k)) fd.set(k, v); });
    return {fd, action: f.getAttribute('action') || ''};
  }
  const cboxRows = (root, name) => [...root.querySelectorAll('input[name="' + name + '"]')].map(c => c.value);
  // The last list page read per module and category, reused by the next post
  // for its form (the hidden fields don't change), so a post doesn't re-open
  // the list. Kept for two minutes.
  const lastList = {};
  const remember = (key, doc) => { lastList[key] = {doc, at: Date.now()}; return doc; };
  const recalled = key => { const c = lastList[key]; return c && Date.now() - c.at < 120000 ? c.doc : null; };
  const chunks = (ids, n) => { const out = []; for (let i = 0; i < ids.length; i += n) out.push(ids.slice(i, i + n)); return out; };
  const result = () => ({ok: [], failed: {}});
  const failAll = (res, ids, why) => ids.forEach(i => { res.failed[i] = why; });

  // ------------------------------------------------------------ classic items
  // The generic bulk post (the site's BulkActionProcess): strBulkAction =
  // Publish / Unpublish / Delete, IDs under cboxUnpublished (publish, delete)
  // or cboxPublished (unpublish).
  async function classicBulk(bf, postUrl, action, ids, extra) {
    const res = result();
    if (!ids.length) return res;
    const verb = {publish: 'Publish', unpublish: 'Unpublish', delete: 'Delete'}[action];
    const field = action === 'unpublish' ? 'cboxPublished' : 'cboxUnpublished';
    for (const part of chunks(ids, 150)) {
      const fd = new FormData();
      for (const [k, v] of bf.fd.entries()) fd.append(k, v);
      Object.entries(extra || {}).forEach(([k, v]) => fd.set(k, String(v)));
      fd.set('strBulkAction', verb);
      part.forEach(i => fd.append(field, i));
      const r = await post(postUrl, fd);
      if (r.ok) res.ok.push(...part); else failAll(res, part, 'HTTP ' + r.status);
    }
    return res;
  }

  // Bids, FAQ, News Flash, Archive Center: a category list page with one
  // form, an item list opened by posting that form, and a bulk form on it.
  function classic(cfg) {
    const m = {
      async cats() {
        const doc = await getDoc(cfg.path);
        const seen = new Map();
        doc.querySelectorAll('select[onchange]').forEach(s => {
          const oc = s.getAttribute('onchange') || '';
          const mm = oc.match(cfg.catRe);
          if (!mm) return;
          const id = mm[1];
          if (seen.has(id)) return;
          const row = s.closest('tr') || s.parentElement;
          const a = [...row.querySelectorAll('a')].find(x => clean(x.textContent) && !/OrderSingle/.test(x.getAttribute('onclick') || ''));
          const opts = [...s.options].map(o => clean(o.text).toLowerCase());
          const published = opts.includes('unpublish') ? true : opts.includes('publish') ? false : null;
          seen.set(id, {id: Number(id), name: a ? clean(a.textContent).replace(/^\*\s*/, '') : '', published});
        });
        return [...seen.values()];
      },
      async listDoc(cid, page) {
        const entries = await pageForm(cfg.path, cfg.form);
        const fd = fdFrom(entries, Object.assign({}, cfg.itemList(cid), page && cfg.pageField ? {[cfg.pageField]: page} : {}));
        const r = await post(cfg.post, fd);
        if (!r.ok) throw new Error('item list did not open (HTTP ' + r.status + ')');
        return parse(r.text);
      },
      async items(cid) {
        const doc = remember(cfg.path + cid, await m.listDoc(cid));
        const out = [];
        const add = (name, pub) => doc.querySelectorAll('input[name="' + name + '"]').forEach(c => {
          const row = c.closest('tr') || c.parentElement;
          const a = row && [...row.querySelectorAll('a')].find(x => clean(x.textContent));
          out.push({id: Number(c.value), name: a ? clean(a.textContent) : '', published: pub});
        });
        add('cboxPublished', true);
        add('cboxUnpublished', false);
        return out;
      },
      async catAct(action, ids) {
        const res = result();
        for (const id of ids) {
          try {
            const entries = await pageForm(cfg.path, cfg.form);
            const r = await post(cfg.post, fdFrom(entries, Object.assign({}, NO_SEND, cfg.catFields(cfg.catPages[action], id))));
            if (r.ok) res.ok.push(id); else res.failed[id] = 'HTTP ' + r.status;
          } catch (e) { res.failed[id] = String(e.message || e); }
        }
        return res;
      },
      async itemAct(action, cid, ids) {
        const doc = recalled(cfg.path + cid) || remember(cfg.path + cid, await m.listDoc(cid));
        const bf = bulkForm(doc, cfg.bulkSel);
        if (!bf) { const res = result(); failAll(res, ids, 'no bulk form on the item list'); return res; }
        return classicBulk(bf, cfg.post, action, ids, NO_SEND);
      },
    };
    return m;
  }

  const bids = classic({
    path: '/Admin/Bids.aspx', post: '/admin/Bids.aspx', form: 'form[name="frmBidCategory"]',
    catRe: /BidCategoryModifyDelete\(\s*this\s*,\s*(\d+)/,
    catPages: {publish: 'BidCategoryPublish', unpublish: 'BidCategoryUnpublish', delete: 'BidCategoryDelete'},
    catFields: (page, id) => ({strPage: page, ysnSave: '1', lngBidCategoryID: id, lngContainerID: ''}),
    itemList: id => ({strPage: 'BidItemList', ysnSave: '0', lngBidCategoryID: id, lngContainerID: ''}),
    bulkSel: 'form[name="frmBidBulkActions"]',
  });
  const faq = classic({
    path: '/Admin/FAQ.aspx', post: '/admin/FAQ.aspx', form: 'form[name="frmFAQTopicList"]',
    catRe: /FAQTopicModifyDelete\(\s*(\d+)\s*,\s*this/,
    catPages: {publish: 'CategoryPublish', unpublish: 'CategoryUnpublish', delete: 'CategoryDelete'},
    catFields: (page, id) => ({strPage: page, ysnSave: '1', lngFAQTopicID: id, lngContainerID: ''}),
    itemList: id => ({strPage: 'ItemList', ysnSave: '0', lngFAQTopicID: id, lngContainerID: ''}),
    bulkSel: 'form[name="frmFAQQuestionBulkActions"]',
  });
  const news = classic({
    path: '/Admin/CivicAlerts.aspx', post: '/admin/CivicAlerts.aspx', form: 'form[name="frmAlertDetail"]',
    catRe: /CallAlertCategoryDetail\(\s*(\d+)\s*,\s*this/,
    catPages: {publish: 'CivicAlertCategoryPublish', unpublish: 'CivicAlertCategoryUnpublish', delete: 'CivicAlertCategoryDelete'},
    catFields: (page, id) => ({strPage: page, ysnSave: '1', ysnDeleted: '0', lngCivicAlertCategoryID: id, lngContainerID: ''}),
    itemList: id => ({strPage: 'CivicAlertItemList', ysnSave: '0', lngCivicAlertCategoryID: id, lngContainerID: ''}),
    bulkSel: 'form[name="frmBulkActions"]',
  });
  const archive = classic({
    path: '/Admin/Archive.aspx', post: '/admin/archive.aspx', form: 'form[name="frmArchiveMasterList"]',
    catRe: /ModifyArchiveMaster\(\s*this\s*,\s*(\d+)/,
    catPages: {publish: 'ArchiveMasterPublish', unpublish: 'ArchiveMasterUnpublish', delete: 'ArchiveMasterDelete'},
    catFields: (page, id) => ({strPage: page, ysnSave: '1', lngArchiveMasterID: id, lngContainerID: ''}),
    itemList: id => ({strPage: 'ArchiveItemList', ysnSave: '0', lngArchiveMasterID: id, lngContainerID: ''}),
    bulkSel: 'form[name="frmArchiveBulkActions"]',
  });

  // ---------------------------------------------------------------- Calendar
  // Calendars (categories) and events both go through the page's own bulk
  // form (aspnetForm). The event list hides past events unless the view
  // option "Previous" is posted as the checkbox pair true,false.
  const CAL_TARGET = 'ctl00$ctl00$adminContent$pageContent$calendarAdmin$contentUpdatePanel';
  const calendar = {
    async listPage(cid) {
      const html = await frameHtml('/admin/calendar.aspx?CID=' + cid);
      const doc = parse(html);
      const f = doc.getElementById('aspnetForm');
      if (!f) throw new Error('Calendar list did not load');
      const fd = new FormData(f);
      fd.delete('Previous'); fd.append('Previous', 'true'); fd.append('Previous', 'false');
      fd.set('fromViewOptions', '1');
      const r = await post('/admin/calendar.aspx?CID=' + cid, fd);
      if (!r.ok) throw new Error('Calendar list did not load (HTTP ' + r.status + ')');
      return parse(r.text);
    },
    async cats() {
      const doc = parse(await frameHtml('/admin/calendar.aspx?show=all'));
      const pub = new Set(cboxRows(doc, 'cboxPublished')), unpub = new Set(cboxRows(doc, 'cboxUnpublished'));
      const seen = new Map();
      doc.querySelectorAll('a[onclick*="displayItemList"]').forEach(a => {
        const id = ((a.getAttribute('onclick') || '').match(/displayItemList\(\s*([1-9]\d*)/) || [])[1];
        if (id && !seen.has(id) && clean(a.textContent))
          seen.set(id, {id: Number(id), name: clean(a.textContent), published: pub.has(id) ? true : unpub.has(id) ? false : null});
      });
      return [...seen.values()];
    },
    async items(cid) {
      const out = [], seen = new Set();
      let doc = remember('cal' + cid, await calendar.listPage(cid));
      for (let page = 1; page < 200; page++) {
        const before = seen.size;
        [['cboxPublished', true], ['cboxUnpublished', false]].forEach(([n, p]) => doc.querySelectorAll('input[name="' + n + '"]').forEach(c => {
          if (seen.has(c.value)) return;
          seen.add(c.value);
          const row = c.closest('.item') || c.parentElement;
          const t = row && row.querySelector('[id^=QEeventTitle], h3, a');
          out.push({id: Number(c.value), name: t ? clean(t.textContent) : '', published: p,
                    series: row && row.getAttribute ? row.getAttribute('data-series') || '' : ''});
        }));
        const pager = doc.querySelector('p.pagination');
        const next = pager && [...pager.querySelectorAll('a')].find(a => clean(a.textContent) === String(page + 1));
        if (!next || seen.size === before) break;
        const m = (next.getAttribute('href') || '').match(/__doPostBack\('([^']+)','([^']*)'\)/);
        if (!m) break;
        const fd = new FormData(doc.getElementById('aspnetForm'));
        fd.delete('Previous'); fd.append('Previous', 'true'); fd.append('Previous', 'false');
        fd.set('__EVENTTARGET', m[1]); fd.set('__EVENTARGUMENT', m[2]);
        const r = await post('/admin/calendar.aspx?CID=' + cid, fd);
        if (!r.ok) break;
        doc = parse(r.text);
      }
      return out;
    },
    async bulk(path, doc, verb, field, ids, extra) {
      const res = result();
      const f = doc.getElementById('aspnetForm');
      for (const part of chunks(ids, 150)) {
        const fd = new FormData(f);
        [...new Set([...fd.keys()])].filter(k => /^cbox/.test(k)).forEach(k => fd.delete(k));
        Object.entries(NO_SEND).forEach(([k, v]) => fd.set(k, v));
        Object.entries(extra || {}).forEach(([k, v]) => fd.set(k, String(v)));
        fd.set('strBulkAction', verb); fd.set('quickEditAction', '');
        part.forEach(i => fd.append(field, i));
        const r = await post(path, fd);
        if (r.ok) res.ok.push(...part); else failAll(res, part, 'HTTP ' + r.status);
      }
      return res;
    },
    async catAct(action, ids) {
      const doc = parse(await frameHtml('/admin/calendar.aspx?show=all'));
      const verb = {publish: 'PublishCat', unpublish: 'UnpublishCat', delete: 'DeleteCat'}[action];
      const field = action === 'unpublish' ? 'cboxPublished' : 'cboxUnpublished';
      // The page's Delete is DeleteCatWithNoItems: the IDs also go in deletableIDs.
      return calendar.bulk('/admin/calendar.aspx?show=all', doc, verb, field, ids,
                           action === 'delete' ? {deletableIDs: ids.join(',')} : {});
    },
    async itemAct(action, cid, ids) {
      const doc = recalled('cal' + cid) || remember('cal' + cid, await calendar.listPage(cid));
      const verb = {publish: 'Publish', unpublish: 'Unpublish', delete: 'Delete'}[action];
      const field = action === 'unpublish' ? 'cboxPublished' : 'cboxUnpublished';
      // Recurring events: each occurrence is its own ID; EditSeries false = just these.
      return calendar.bulk('/admin/calendar.aspx?CID=' + cid, doc, verb, field, ids,
                           {EditSeries: 'false', SelectedSeries: ''});
    },
  };

  // --------------------------------------------------------------- Form Center
  const formcenter = {
    async cats() {
      const doc = await getDoc('/Admin/FormCenter');
      const st = new Map();
      cboxRows(doc, 'cboxPublishedCategory').forEach(i => st.set(i, true));
      cboxRows(doc, 'cboxUnpublishedCategory').forEach(i => st.set(i, false));
      // The built-in "Sample Forms" category (sampleCategoryCheckbox) can't be deleted.
      const sample = new Set([...doc.querySelectorAll('input.sampleCategoryCheckbox')].map(c => c.value));
      const seen = new Map();
      doc.querySelectorAll('h3 a[href*="/Category/Detail/"]').forEach(a => {
        const id = (a.getAttribute('href').match(/Detail\/(\d+)/) || [])[1];
        if (id && !seen.has(id)) seen.set(id, {id: Number(id), name: clean(a.textContent), published: st.has(id) ? st.get(id) : null,
                                               sample: sample.has(id)});
      });
      return [...seen.values()];
    },
    async items(cid) {
      const out = [], seen = new Set();
      for (let p = 1; p < 100; p++) {
        const r = await http('/Admin/FormCenter/Item/ItemList?categoryID=' + cid + '&page=' + p);
        if (!r.ok) throw new Error('form list did not load (HTTP ' + r.status + ')');
        const doc = parse(r.text);
        const before = seen.size;
        [['cboxPublishedItem', true], ['cboxUnpublishedItem', false]].forEach(([n, pub]) => cboxRows(doc, n).forEach(id => {
          if (seen.has(id)) return; seen.add(id);
          const c = doc.querySelector('input[name="' + n + '"][value="' + id + '"]');
          const row = c.closest('.item, tr, li') || c.parentElement;
          const a = row.querySelector('h3 a, a');
          out.push({id: Number(id), name: a ? clean(a.textContent) : '', published: pub});
        }));
        if (seen.size === before || !doc.querySelector('a[href*="page=' + (p + 1) + '"], [onclick*="' + (p + 1) + '"]')) break;
      }
      return out;
    },
    async catAct(action, ids) {
      const res = result();
      const verb = {publish: 'BulkPublish', unpublish: 'BulkUnpublish', delete: 'BulkDelete'}[action];
      for (const part of chunks(ids, 150)) {
        const a = form({bulkActionCategoryIDList: part.join(',') + ','});
        const r = await post('/Admin/FormCenter/Category/' + verb, a.body, a.headers);
        if (r.ok) res.ok.push(...part); else failAll(res, part, 'HTTP ' + r.status);
      }
      return res;
    },
    async itemAct(action, cid, ids) {
      const res = result();
      const verb = {publish: 'BulkPublish', unpublish: 'BulkUnpublish', delete: 'BulkDelete'}[action];
      for (const part of chunks(ids, 150)) {
        const a = form({itemActionIDList: part.join(',') + ',', categoryID: cid});
        const r = await post('/Admin/FormCenter/Item/' + verb, a.body, a.headers);
        if (r.ok) res.ok.push(...part); else failAll(res, part, 'HTTP ' + r.status);
      }
      return res;
    },
  };

  // ---------------------------------------------------------------- Facilities
  // Each row carries a "possible actions" bit value (1 publish, 2 unpublish,
  // 16 delete ...) that the page posts back with the action.
  const facilities = {
    rows(doc) {
      return [...doc.querySelectorAll('input[name=categoryBulkActionSelectOne]')].map(c => {
        const row = c.closest('.item') || c.parentElement;
        const flags = parseInt((row.querySelector('input[name=hdnCategoryPossibleBulkActions]') || {}).value, 10);
        const a = row.querySelector('h3 a, p.name a');
        return {id: Number(c.value), name: clean(a ? a.textContent : ''), published: isNaN(flags) ? null : !!(flags & 2), flags};
      });
    },
    // The page's own bulk call (BulkActionsCommon.js): CanDo (GET) then Do
    // (POST), every ID at once. kind: '' for categories, 'Item' for facilities.
    async bulk(kind, parent, rows, bit) {
      const res = result();
      for (const part of chunks(rows, 150)) {
        const ids = part.map(r => r.id);
        const q = new URLSearchParams({id: parent == null ? '' : parent, ids: ids.join(','), bulkAction: bit});
        part.forEach(r => q.append('possibleActionsValues', r.flags));
        const can = await http('/Admin/Facilities/Category/CanDoBulk' + kind + 'Action?' + q + '&_=' + Date.now(), {headers: XHR});
        let data = {};
        try { data = JSON.parse(can.text).Data || {}; } catch (e) {}
        if (data.CanDo !== true) {
          failAll(res, ids, clean(data.Error || data.CustomError || (data.StatusChanged ? 'changed on the site since it was read' : 'the site said no (HTTP ' + can.status + ')')));
          continue;
        }
        q.set('refreshOnly', 'false');
        const r = await post('/Admin/Facilities/Category/DoBulk' + kind + 'Action', q.toString(),
                             Object.assign({'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}, XHR));
        if (r.ok) res.ok.push(...ids); else failAll(res, ids, 'HTTP ' + r.status);
      }
      return res;
    },
    async cats() { return facilities.rows(await getDoc('/Admin/Facilities')); },
    async items(cid) {
      const out = [], seen = new Set();
      for (let p = 1; p < 100; p++) {
        const r = await http('/Admin/Facilities/Category/Detail/' + cid + (p > 1 ? '?page=' + p : ''));
        if (!r.ok) throw new Error('facility list did not load (HTTP ' + r.status + ')');
        const rows = facilities.rows(parse(r.text)).filter(x => !seen.has(x.id));
        rows.forEach(x => { seen.add(x.id); out.push(x); });
        if (!rows.length || !/page=\d/.test(r.text)) break;
      }
      return out;
    },
    async catAct(action, ids) {
      const all = new Map((await facilities.cats()).map(c => [c.id, c]));
      const res = result();
      const rows = ids.map(Number).filter(i => all.has(i) || !(res.failed[i] = 'not on the site')).map(i => all.get(i));
      const bit = {publish: 1, unpublish: 2, delete: 16}[action];
      const r = await facilities.bulk('', null, rows, bit);
      res.ok.push(...r.ok);
      // The site refuses a whole batch for one category it won't take: try those one by one.
      const again = rows.filter(x => r.failed[x.id] !== undefined);
      for (const x of (again.length > 1 ? again : [])) {
        const one = await facilities.bulk('', null, [x], bit);
        res.ok.push(...one.ok);
        Object.assign(res.failed, one.failed);
      }
      if (again.length === 1) Object.assign(res.failed, r.failed);
      return res;
    },
    async itemAct(action, cid, ids) {
      // Bulk first (one request for every facility); one at a time for any the bulk call refused.
      const all = new Map((await facilities.items(cid)).map(c => [c.id, c]));
      const res = result();
      const rows = ids.map(Number).filter(i => all.has(i) || !(res.failed[i] = 'not in this category')).map(i => all.get(i));
      const b = await facilities.bulk('Item', cid, rows, {publish: 1, unpublish: 2, delete: 16}[action]);
      res.ok.push(...b.ok);
      const retry = Object.keys(b.failed).map(Number);
      let one = await facilities.itemOne(action, cid, retry);
      // A facility with sub-facilities can only go once they have: try what was refused once more.
      const later = Object.keys(one.failed).map(Number);
      if (action === 'delete' && later.length && one.ok.length) {
        const two = await facilities.itemOne(action, cid, later);
        one = {ok: one.ok.concat(two.ok), failed: two.failed};
      }
      res.ok.push(...one.ok);
      Object.entries(one.failed).forEach(([k, v]) => { res.failed[k] = v + (b.failed[k] ? ' (bulk: ' + b.failed[k] + ')' : ''); });
      return res;
    },
    async itemOne(action, cid, ids) {
      const res = result();
      for (const fid of ids) {
        if (action === 'delete') {
          const a = form({});
          const c = await post('/Admin/Facilities/Item/CanBeDeleted/' + fid, a.body, a.headers);
          let code = -1;
          try { code = JSON.parse(c.text || '{}').Deletable; } catch (e) {}
          if (code !== 0) { res.failed[fid] = 'the site says it can\'t be deleted (sub-facilities or reservations; code ' + code + ')'; continue; }
          const r = await post('/Admin/Facilities/Item/Delete/' + fid, a.body, a.headers);
          let ok = r.ok;
          try { ok = ok && JSON.parse(r.text).Deleted !== false; } catch (e) {}
          if (ok) res.ok.push(fid); else res.failed[fid] = 'HTTP ' + r.status;
        } else {
          const a = form({id: fid, screenFrom: 'ItemList', statusID: action === 'publish' ? 40 : 10, categoryID: cid,
                          isParentFacility: 'False', seoName: '', isAllCategoriesSearch: 'false', pageNumber: 1});
          const r = await post('/Admin/Facilities/Item/UpdateStatus/', a.body, a.headers);
          if (r.ok) res.ok.push(fid); else res.failed[fid] = 'HTTP ' + r.status;
        }
      }
      return res;
    },
  };

  // ------------------------------------------------------- Resource Directory
  // One flat list (no categories), 100 a page per section, and one bulk form.
  const resdir = {
    async pages() {
      const first = parse(await frameHtml('/admin/BusinessDirectoryii.aspx'));
      return first;
    },
    async items() {
      // Every entry: read the first page, then post the bulk form's paging.
      const doc = remember('resdir', await resdir.pages());
      const out = new Map();
      const add = d => [['cboxPublished', true], ['cboxUnpublished', false]].forEach(([n, p]) => d.querySelectorAll('input[name="' + n + '"]').forEach(c => {
        const row = c.closest('tr') || c.parentElement;
        const a = row && [...row.querySelectorAll('a')].find(x => clean(x.textContent));
        out.set(c.value, {id: Number(c.value), name: a ? clean(a.textContent) : '', published: p});
      }));
      add(doc);
      // "1 - 100 of 403 Listings" under each heading (Unpublished = section 0,
      // Published = 1): 100 a page.
      const html = doc.body.innerHTML, totals = {};
      const heads = [...html.matchAll(/(Un)?[Pp]ublished Resources/g)].map(m => [m.index, m[1] ? 0 : 1]);
      for (const m of html.matchAll(/of\s+(\d+)\s+Listings/g)) {
        const before = heads.filter(([pos]) => pos < m.index);
        if (before.length) totals[before[before.length - 1][1]] = Math.ceil(Number(m[1]) / 100);
      }
      for (const [sec, last] of Object.entries(totals)) {
        for (let p = 2; p <= last; p++) {
          const html = await frameStep('/admin/BusinessDirectoryii.aspx', 'w.changePage(' + p + ', ' + sec + ')');
          add(parse(html));
        }
      }
      return [...out.values()];
    },
    async cats() { return []; },
    async itemAct(action, cid, ids) {
      const doc = recalled('resdir') || remember('resdir', await resdir.pages());
      const bf = bulkForm(doc, 'form[name="frmBusinessDirectoryBulkActions"]');
      if (!bf) { const res = result(); failAll(res, ids, 'no bulk form'); return res; }
      return classicBulk(bf, '/admin/BusinessDirectoryii.aspx', action, ids, NO_SEND);
    },
    async catAct(action, ids) { const res = result(); failAll(res, ids, 'Resource Directory has no categories here'); return res; },
  };
  // Load a classic page in a frame, run one step that navigates, return the next page.
  function frameStep(path, js) {
    return new Promise((resolve, reject) => {
      const f = document.createElement('iframe');
      f.style.cssText = 'position:fixed;left:-9999px;top:0;width:1200px;height:900px;visibility:hidden';
      let stage = 0;
      const timer = setTimeout(() => { f.remove(); reject(new Error('timed out on ' + path)); }, 90000);
      f.onload = () => {
        const w = f.contentWindow;
        try { w.alert = () => {}; w.confirm = () => false; } catch (e) {}
        if (stage++ === 0) { try { (new Function('w', js))(w); } catch (e) { clearTimeout(timer); f.remove(); reject(e); } return; }
        clearTimeout(timer);
        const html = f.contentDocument.documentElement.outerHTML;
        f.remove();
        resolve(html);
      };
      f.src = path;
      document.body.append(f);
    });
  }

  // ---------------------------------------------------------- Staff Directory
  // Departments are the categories, staff the items. Everything posts one
  // form (frmCityDepartmentDetail) to /admin/Directory.aspx. A department's
  // children come from the tree's loader (40 published, 10 unpublished).
  const SD_IMAGES = '/common/images/StaffDirectory/';
  const staff = {
    async base() { return pageForm('/Admin/Directory.aspx', 'form[name="frmCityDepartmentDetail"]'); },
    async act(o) {
      const fd = fdFrom(await staff.base(), Object.assign({}, NO_SEND, o));
      return post('/admin/Directory.aspx', fd);
    },
    async children(parent, level) {
      const entries = await staff.base();
      const mid = (entries.find(([k]) => k === 'lngResourceID') || [0, '4'])[1] || '4';
      const u = '/Common/Admin/CityDirectory/AJAX-Loader.ashx?' + new URLSearchParams({
        nodeParent: parent, level: level || 3, moduleID: mid, imagesPath: SD_IMAGES, usageFlag: 3,
        extraParam1: 0, extraParam2: 0, nodeName: 'DIV', hasItems: 1, isPersonal: 0});
      const x = await http(u);
      if (!x.ok) throw new Error('Staff Directory list did not load (' + x.status + ')');
      const doc = parse('<div>' + x.text + '</div>');
      return [...doc.querySelectorAll('div[id^=div]')].map(e => {
        const a = e.querySelector('a[onmouseover]');
        const mm = (a ? a.getAttribute('onmouseover') : '').match(/add(?:Item)?SubMenus\(\s*1,\s*\d+,\s*'((?:[^'\\]|\\.)*)'/);
        const st = parseInt(e.getAttribute('cnstatus'), 10);
        return {id: Number(e.id.replace(/^div(Item)?/, '')), item: /^divItem/.test(e.id),
                name: mm ? clean(mm[1].replace(/\\'/g, "'")) : '', published: st === 40 ? true : st === 10 ? false : null};
      }).filter(k => k.id);
    },
    async cats() {
      // Every department, nested ones too (depth first), with its parent.
      const out = [];
      const walk = async (pid, depth) => {
        if (depth > 10) return;
        for (const k of await staff.children(pid, depth + 1)) {
          if (k.item) continue;
          out.push({id: k.id, name: k.name, published: k.published, parent: pid});
          await walk(k.id, depth + 1);
        }
      };
      await walk(0, 0);
      return out;
    },
    async items(cid) {
      return (await staff.children(cid)).filter(k => k.item).map(k => ({id: k.id, name: k.name, published: k.published}));
    },
    // Move a department under another (as its last child), or an employee to
    // another department (as its last employee): the tree's own Move mode.
    async moveDept(id, newParent) {
      const r = await staff.act({strPage: 'CategoryMoveEx', lngCityDepartmentID: id, lngDestCityDepartmentID: newParent,
                                 intSibling: -1, lngCityEmployeeID: -1, lngDestCityEmployeeID: -1, intDirection: 1, ysnSave: 1});
      return {ok: r.ok, status: r.status};
    },
    async moveEmployee(id, oldDept, newDept) {
      const r = await staff.act({strPage: 'ItemMoveEx', lngCityDepartmentID: oldDept, lngDestCityDepartmentID: newDept,
                                 lngCityEmployeeID: id, lngDestCityEmployeeID: -1, lngContainerID: '', intDirection: 1, ysnSave: 1});
      return {ok: r.ok, status: r.status};
    },
    // Employees of many departments (one loader request each, one at a time).
    async employees(deptIds) {
      const out = [];
      for (const d of deptIds) {
        for (const k of await staff.children(d)) {
          if (k.item) out.push({id: k.id, dept: Number(d), name: k.name, published: k.published});
        }
      }
      return out;
    },
    async catAct(action, ids, parents) {
      const res = result();
      const page = {publish: 'CategoryPublish', unpublish: 'CategoryUnpublish', delete: 'CategoryDelete'}[action];
      for (const id of ids) {
        const pid = (parents || {})[id] || 0;
        try {
          const r = await staff.act({strPage: page, lngCityDepartmentID: id, intParentID: pid, intMenuItemType: 1, ysnSave: 1, lngContainerID: ''});
          if (r.ok) res.ok.push(id); else res.failed[id] = 'HTTP ' + r.status;
        } catch (e) { res.failed[id] = String(e.message || e); }
      }
      return res;
    },
    async itemAct(action, cid, ids) {
      const res = result();
      const page = {publish: 'ItemPublish', unpublish: 'ItemUnpublish', delete: 'ItemDelete'}[action];
      for (const id of ids) {
        try {
          const r = await staff.act({strPage: page, lngCityEmployeeID: id, lngCityDepartmentID: cid, intParentID: cid,
                                     lngContainerID: '', intTriggeredFrom: 0, intMenuItemType: 2, ysnSave: 1});
          if (r.ok) res.ok.push(id); else res.failed[id] = 'HTTP ' + r.status;
        } catch (e) { res.failed[id] = String(e.message || e); }
      }
      return res;
    },
  };

  // ------------------------------------------------------- create categories
  // The extension's Bulk Create, per module: open the blank Add Category form
  // the way the page does, set the name, save (published, or as a draft).
  // Returns {id} when the site's answer names the new category, else {} and
  // the caller finds it by name.
  function legacyCreate(cfg) {
    return async function (name, publish) {
      const entries = await pageForm(cfg.path, cfg.form);
      const r1 = await post(cfg.post, fdFrom(entries, cfg.step1));
      const f2 = parse(r1.text).querySelector(cfg.editForm);
      if (!f2 || !f2.elements[cfg.nameField]) throw new Error('the Add Category form did not open');
      const fd = new FormData(f2);
      fd.set(cfg.nameField, name);
      Object.entries(publish ? cfg.publish : cfg.draft).forEach(([k, v]) => fd.set(k, v));
      Object.entries(NO_SEND).forEach(([k, v]) => { if (fd.has(k)) fd.set(k, v); });
      const r2 = await post(cfg.post, fd);
      if (!r2.ok) throw new Error('save failed (HTTP ' + r2.status + ')');
      return {};
    };
  }
  bids.create = legacyCreate({
    path: '/Admin/Bids.aspx', post: '/admin/Bids.aspx', form: 'form[name="frmBidCategory"]',
    step1: {strPage: 'BidCategoryForm', ysnSave: '0', lngBidCategoryID: '0', lngContainerID: ''},
    editForm: '#frmBidCategoryInfo', nameField: 'txtBidCategoryName',
    publish: {ysnSaveAndPublish: '1'}, draft: {ysnSaveAndPublish: '0'},
  });
  faq.create = legacyCreate({
    path: '/Admin/FAQ.aspx', post: '/admin/FAQ.aspx', form: 'form[name="frmFAQTopicList"]',
    step1: {strPage: 'CategoryAdd', ysnSave: '0'},
    editForm: 'form[name="FAQFormInfo"]', nameField: 'txtTitle',
    publish: {ysnSave: '1', ysnPublishDetail: '1'}, draft: {ysnSave: '1', ysnPublishDetail: '0'},
  });
  news.create = legacyCreate({
    path: '/Admin/CivicAlerts.aspx', post: '/admin/CivicAlerts.aspx', form: 'form[name="frmAlertDetail"]',
    step1: {lngContainerID: '', lngCivicAlertCategoryID: '0', strPage: 'CivicAlertCategoryAdd', ysnSave: '0'},
    editForm: '#frmAlertCategoryInfo', nameField: 'txtName',
    publish: {ysnPublishCategory: '1'}, draft: {ysnPublishCategory: '0'},
  });
  archive.create = legacyCreate({
    path: '/Admin/Archive.aspx', post: '/admin/archive.aspx', form: 'form[name="frmArchiveMasterList"]',
    step1: {strPage: 'ArchiveMasterForm', ysnSave: '0', lngArchiveMasterID: '0', lngContainerID: ''},
    editForm: 'form[name="frmArchiveMasterForm"]', nameField: 'txtArchiveName',
    publish: {ysnSave: '1', strPage: 'ArchiveMasterSave', ysnPublishDetail: 'true'},
    draft: {ysnSave: '1', strPage: 'ArchiveMasterSave', ysnPublishDetail: 'false'},
  });
  function mvcCreate(base, nameField, publishQuery, draftQuery) {
    return async function (name, publish) {
      const x = await http(base + '/Category/Edit/0');
      const f = parse(x.text).getElementById('aspnetForm');
      if (!f || !f.elements[nameField]) throw new Error('the Add Category form did not open');
      const fd = new FormData(f);
      fd.set(nameField, name);
      const r = await post(base + '/Category/Edit/0' + (publish ? publishQuery : draftQuery), new URLSearchParams(fd));
      const m = r.url.match(/\/Category\/Detail\/(\d+)/i);
      if (!r.ok) throw new Error('save failed (HTTP ' + r.status + ')');
      return m ? {id: Number(m[1])} : {};
    };
  }
  formcenter.create = mvcCreate('/Admin/FormCenter', 'Category.Name', '?saveAction=publish', '?saveAction=save');
  facilities.create = mvcCreate('/Admin/Facilities', 'Category.CategoryName', '?saveType=SavePublish', '?saveType=Save');
  calendar.create = async function (name, publish) {
    const url = '/admin/calendar.aspx?action=editCat&CID=0';
    const doc = parse(await frameHtml('/Admin/Calendar.aspx'));
    const fd = new FormData(doc.getElementById('aspnetForm'));
    fd.set('quickEditAction', '');
    const s1 = await post(url, fd);
    const f2 = parse(s1.text).getElementById('aspnetForm');
    const el = f2 && [...f2.elements].find(e => e.name && e.name.endsWith('$txtCalendarTitle'));
    if (!el) throw new Error('the Add Calendar form did not open');
    const pre = el.name.slice(0, el.name.length - 'txtCalendarTitle'.length);
    const fd2 = new FormData(f2);
    fd2.set(pre + 'txtCalendarTitle', name.slice(0, 50));
    fd2.set('__EVENTTARGET', pre + (publish ? 'btnPublish' : 'btnSave'));
    fd2.set('__EVENTARGUMENT', '');
    Object.entries(NO_SEND).forEach(([k, v]) => { if (fd2.has(k)) fd2.set(k, v); });
    const s2 = await post(url, fd2);
    const m = s2.url.match(/[?&]CID=(\d+)/i);
    if (!s2.ok) throw new Error('save failed (HTTP ' + s2.status + ')');
    return m && m[1] !== '0' ? {id: Number(m[1])} : {};
  };
  staff.create = async function (name, publish, parent) {
    const entries = await staff.base();
    const r1 = await post('/admin/Directory.aspx', fdFrom(entries, {strPage: 'CategoryForm', ysnSave: '0', lngCityDepartmentID: '0',
                                                                     intParentID: parent || '-1', intSibling: '-1', intDirection: '1'}));
    const f2 = parse(r1.text).querySelector('form[name="frmCityDepartmentInfo"]');
    if (!f2 || !f2.elements.txtDepartmentName) throw new Error('the Add Department form did not open');
    const fd = new FormData(f2);
    fd.set('txtDepartmentName', name);
    Object.entries({ysnSave: '1', strPage: 'CategoryInsertModify', ysnTryPublish: publish ? '1' : '0', ysnTrySubmit: '0'}).forEach(([k, v]) => fd.set(k, v));
    const r2 = await post('/admin/Directory.aspx', fd);
    if (!r2.ok) throw new Error('save failed (HTTP ' + r2.status + ')');
    return {};
  };

  // -------------------------------------------------------------------- Pages
  // Every page, published or not: /Pages/Version/PageList (one request) lists
  // them all. A page is read as the logged-in admin sees it with Live Edit on
  // (the enableLiveEdit / showAll cookies, set for the read and put back):
  // unpublished pages then render instead of 404, and the breadcrumbs carry
  // the path and the status ("[DRAFT] City Council").
  // Actions are the ones Live Edit uses: /Pages/VersionStatus/Publish/{version},
  // /Pages/PageStatus/Unpublish and /Pages/PageStatus/Delete (refused while the
  // page has sub-pages).
  const PAGE_COOKIES = ['enableLiveEdit', 'showAll'];
  const MODULE_PATHS = [
    [/^\/AgendaCenter/i, 'Agenda Center'], [/^\/(calendar\.aspx|m\/calendar|Calendar\b)/i, 'Calendar'],
    [/^\/(CivicAlerts\.aspx|m\/NewsFlash)/i, 'News Flash'], [/^\/(FAQ\.aspx|m\/faq)/i, 'FAQs'],
    [/^\/Bids\.aspx/i, 'Bids'], [/^\/Archive\.aspx/i, 'Archive Center'], [/^\/(FormCenter|Forms\b)/i, 'Form Center'],
    [/^\/Facilities/i, 'Facilities'], [/^\/Directory\.aspx/i, 'Staff Directory'],
    [/^\/BusinessDirectoryii?\.aspx/i, 'Resource Directory'], [/^\/QuickLinks\.aspx/i, 'Quick Links'],
    [/^\/Jobs(II)?\.aspx/i, 'Jobs'], [/^\/Blog\.aspx/i, 'Blog'], [/^\/list\.aspx/i, 'Notify Me'],
    [/^\/(Activities|RecDesk)/i, 'Activities'], [/^\/PhotoGallery/i, 'Photo Gallery'],
  ];
  function linkType(href) {
    const h = (href || '').trim();
    if (!h || h.startsWith('#') || /^javascript:/i.test(h)) return null;
    if (/^mailto:/i.test(h)) return ['email', ''];
    if (/^(tel|sms|fax):/i.test(h)) return null;
    let u;
    try { u = new URL(h, location.origin); } catch (e) { return ['broken', '']; }
    if (u.host !== location.host) return ['external', u.host];
    const p = u.pathname;
    if (/^\/DocumentCenter\/(View|Home\/View)\//i.test(p) || /\.(pdf|docx?|xlsx?|pptx?|zip|csv|txt|rtf)$/i.test(p)) return ['file', ''];
    if (/^\/ImageRepository\//i.test(p) || /\.(jpe?g|png|gif|svg|webp|bmp)$/i.test(p)) return ['image file', ''];
    if (/^\/\d+\//.test(p) || p === '/') return ['page', ''];
    if (/^\/DocumentCenter/i.test(p)) return ['module', 'Document Center'];
    for (const [re, name] of MODULE_PATHS) if (re.test(p)) return ['module', name];
    return ['site link', ''];
  }
  const STATUS_RE = /^\[([A-Za-z ]+)\]\s*/;
  function readableText(el) {
    const c = el.cloneNode(true);
    c.querySelectorAll('script, style').forEach(x => x.remove());
    c.querySelectorAll('br').forEach(x => x.replaceWith('\n'));
    c.querySelectorAll('p, div, li, h1, h2, h3, h4, h5, h6, tr, table, ul, ol, blockquote').forEach(x => x.append('\n'));
    return (c.textContent || '').split('\n').map(l => l.replace(/[ \t\u00a0]+/g, ' ').trim()).filter(Boolean).join('\n');
  }
  function parsePage(id, html) {
    const d = parse(html);
    const title = clean((d.querySelector('title') || {}).textContent);
    if (/^Custom404\b/i.test(title)) return {id, status: 'not found', title: '', error: 'the site shows its 404 page'};
    const crumbs = [...d.querySelectorAll('[data-cpRole="breadCrumbs"] a, [data-cprole="breadCrumbs"] a')]
      .filter(a => !/ViewArchives/i.test(a.getAttribute('href') || ''));
    const steps = crumbs.map(a => {
      const href = a.getAttribute('href') || '';
      const m = href.match(/^\/(\d+)\//);
      const text = clean(a.textContent);
      const st = text.match(STATUS_RE);
      return {id: m ? Number(m[1]) : href === '/' ? 1 : null, name: text.replace(STATUS_RE, ''), status: st ? st[1].toLowerCase() : 'published'};
    });
    const last = steps.length ? steps[steps.length - 1] : null;
    const up = steps.slice(0, -1).filter(s => s.id && s.id !== id);
    const main = d.querySelector('[data-cpRole="mainContentContainer"], [data-cprole="mainContentContainer"]') || d.body;
    const skip = el => !!el.closest('[class*="liveEdit"], [id*="liveEdit"], .handle, [id^="modalContainer"], .cpToolbar, p.linkedVersion');
    const linkNote = d.querySelector('p.linkedVersion a[href]');
    const linkTo = linkNote ? linkNote.getAttribute('href') : '';
    const wtype = w => w.getAttribute('data-modulewidgettype') || (w.getAttribute('data-widget-controller-path') || '').replace(/^\/(Layout\/)?/, '').replace(/\/Widget$/i, '');
    const widgets = [...main.querySelectorAll('[data-widget-controller-path]')].filter(w => !skip(w)).map(w => ({
      type: wtype(w), title: clean(((w.querySelector('.widgetTitle, h2, h3') || {}).textContent) || '').slice(0, 120)}));
    const widgetOf = el => { const w = el.closest('[data-widget-controller-path]'); return w ? wtype(w) : ''; };
    const links = [];
    main.querySelectorAll('a[href]').forEach(a => {
      if (skip(a)) return;
      const href = a.getAttribute('href');
      const t = linkType(href);
      if (!t) return;
      links.push({type: t[0], to: t[1], href, text: clean(a.textContent || a.getAttribute('title') || '').slice(0, 200), widget: widgetOf(a)});
    });
    const images = [];
    main.querySelectorAll('img[src]').forEach(i => {
      const src = i.getAttribute('src');
      if (skip(i) || /^\/App_Themes\//i.test(src)) return;
      images.push({src, alt: clean(i.getAttribute('alt') || ''), widget: widgetOf(i)});
    });
    const blocks = [...main.querySelectorAll('[id^="divEditor"]')].filter(b => !skip(b)).map(b => ({
      id: b.id.replace(/^divEditor/, ''), html: b.innerHTML.trim().slice(0, 200000), text: readableText(b).slice(0, 50000),
      links: b.querySelectorAll('a[href]').length, images: b.querySelectorAll('img').length}));
    let status = last ? last.status : 'published';
    if (status === 'link' || linkTo) status = 'linked page';
    const lt = linkTo ? (linkType(linkTo) || ['site link', '']) : null;
    return {id, title: last ? last.name : title.replace(/\s*[|•].*$/, ''), status,
            linkTo, linkToType: lt ? lt[0] + (lt[1] ? ': ' + lt[1] : '') : '',
            parent: up.length ? up[up.length - 1].id : null, path: up.map(s => s.name), widgets, links, images, blocks,
            version: Number((html.match(/id="hdnVersionID"[^>]*value="(\d+)"/) || html.match(/value="(\d+)"[^>]*id="hdnVersionID"/) || [])[1] || 0)};
  }
  async function withPageCookies(fn, on) {
    const prev = {};
    PAGE_COOKIES.forEach(n => { const c = document.cookie.split('; ').find(x => x.startsWith(n + '=')); prev[n] = c ? c.slice(n.length + 1) : undefined; });
    PAGE_COOKIES.forEach(n => { document.cookie = on === false ? n + '=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/' : n + '=true; path=/'; });
    try { return await fn(); } finally {
      PAGE_COOKIES.forEach(n => {
        document.cookie = prev[n] === undefined ? n + '=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/' : n + '=' + prev[n] + '; path=/';
      });
    }
  }
  const pages = {
    async list() {
      const r = await http('/Pages/Version/PageList?pageID=1');
      if (!r.ok) throw new Error('page list did not load (HTTP ' + r.status + ')');
      const all = JSON.parse(r.text).map(p => ({id: Number(p.value), slug: String(p.label).replace(/^\d+\//, '')}));
      // The list leaves out the page it's asked from (pageID=1, the home page): put it back.
      if (!all.some(p => p.id === 1)) all.unshift({id: 1, slug: 'Home'});
      return all;
    },
    // Read pages, 6 at a time (reads only). Returns one parsed page per id.
    // liveEdit false: the quick plain read (about 4x faster), where an
    // unpublished page comes back as the 404 page ("not found").
    async read(ids, liveEdit) {
      return withPageCookies(async () => {
        const out = new Array(ids.length);
        let next = 0;
        const worker = async () => {
          while (next < ids.length) {
            const i = next++;
            try {
              const r = await http('/' + ids[i]);
              out[i] = r.ok ? parsePage(Number(ids[i]), r.text) : {id: Number(ids[i]), status: 'not found', error: 'HTTP ' + r.status};
            } catch (e) { out[i] = {id: Number(ids[i]), error: String(e.message || e)}; }
          }
        };
        await Promise.all(Array.from({length: Math.min(6, ids.length)}, worker));
        // Every request failing at the network level means the site sent us to
        // its sign-in page (another host, so fetch refuses): the login expired.
        if (out.length && out.every(p => /Failed to fetch|logged out/i.test(p.error || ''))) throw new Error('logged out');
        return out;
      }, liveEdit !== false);
    },
    async cats() { return (await pages.list()).map(p => ({id: p.id, name: p.slug, published: null})); },
    // The stylesheets a page loads (absolute addresses), for previews that look like the site.
    async styles(id) {
      const r = await http('/' + (id || 1));
      const d = parse(r.text);
      return [...d.querySelectorAll('link[rel~="stylesheet"][href]')].map(l => new URL(l.getAttribute('href'), location.origin).href);
    },
    async items() { return []; },
    async catAct(action, ids) {
      const res = result();
      const H = Object.assign({'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}, XHR);
      const json = t => { try { return JSON.parse(t); } catch (e) { return null; } };
      for (const id of ids) {
        try {
          if (action === 'publish') {
            const p = (await pages.read([id]))[0];
            if (!p.version) { res.failed[id] = p.error || 'no version to publish'; continue; }
            const wc = await http('/Pages/Version/HasWorkingCopy?versionID=' + p.version, {headers: XHR});
            if (/true/i.test(wc.text)) { res.failed[id] = 'it has an unsaved working copy: publish it in Live Edit'; continue; }
            const r = await post('/Pages/VersionStatus/Publish/' + p.version + '?pageID=' + id + '&workingCopyAction=1', '', H);
            const j = json(r.text);
            if (r.ok && !(j && j.ErrorMessage)) res.ok.push(id); else res.failed[id] = (j && j.ErrorMessage) || 'HTTP ' + r.status;
          } else {
            if (action === 'delete') {
              const s = json((await post('/Pages/PageStatus/HasSubPages?pageID=' + id, '', H)).text);
              if (s && s.hasSubPages) { res.failed[id] = 'it still has sub-pages'; continue; }
            }
            const r = await post('/Pages/PageStatus/' + (action === 'delete' ? 'Delete' : 'Unpublish') + '?pageID=' + id, '', H);
            const j = json(r.text);
            if (r.ok && !(j && j.ErrorMessage)) res.ok.push(id); else res.failed[id] = (j && j.ErrorMessage) || 'HTTP ' + r.status;
          }
        } catch (e) { res.failed[id] = String(e.message || e); }
      }
      return res;
    },
    async itemAct(action, cid, ids) { const res = result(); failAll(res, ids, 'links and images are changed by editing the page'); return res; },
    // Move a page under another page (as its child): Live Edit's own move.
    async move(id, parent) {
      const r = await post('/Pages/PageStatus/Move/' + id + '?parentID=' + parent + '&refPageID=' + parent + '&position=Child', '', XHR);
      let j = null; try { j = JSON.parse(r.text); } catch (e) {}
      return {ok: r.ok && !!(j && j.Success), why: (j && (j.ErrorMessage || j.Message)) || (r.ok ? '' : 'HTTP ' + r.status)};
    },
  };

  // ------------------------------------------- Document Center / Public Images
  // The folder tree and the document lists, the calls the Document Center page
  // makes (the WebCentral Relay extension's folder tools use the same ones).
  // loadSource 0 = Document Center (root folder 1), 4 = Public Images (its
  // root is the one child of ?root=1&type=1).
  const DJ = Object.assign({'Content-Type': 'application/json; charset=UTF-8', Accept: 'application/json'}, XHR);
  const docs = {
    async kids(parent, src) {
      const r = await http('/admin/DocumentCenter/Folder/_AjaxLoadingReact?type=0', {method: 'POST', headers: DJ,
        body: JSON.stringify({value: String(parent), expandTree: false, loadSource: src, selectedFolder: Number(parent)})});
      if (!r.ok) throw new Error('folder list did not load (HTTP ' + r.status + ')');
      return (JSON.parse(r.text).Data || []).map(d => ({id: Number(d.Value), name: clean(d.Text), more: !!d.LoadOnDemand}));
    },
    async roots() {
      const out = [{id: 1, name: 'Document Center', src: 0}];
      try {
        const r = await http('/admin/DocumentCenter/Folder/_AjaxLoadingReact?root=1&type=1', {method: 'POST', headers: DJ,
          body: JSON.stringify({value: '1', expandTree: false, loadSource: 4, selectedFolder: 1})});
        const d = (JSON.parse(r.text).Data || [])[0];
        if (d) out.push({id: Number(d.Value), name: 'Public Images', src: 4});
      } catch (e) {}
      return out;
    },
    // Every folder under a root, one level at a time (one request per folder with children).
    async folders(root, src) {
      const out = [];
      let level = [{id: root, path: []}];
      while (level.length && out.length < 50000) {
        const next = [];
        for (const p of level) {
          for (const k of await docs.kids(p.id, src)) {
            out.push({id: k.id, name: k.name, parent: p.id, path: p.path});
            if (k.more) next.push({id: k.id, path: p.path.concat(k.name)});
          }
        }
        level = next;
      }
      return out;
    },
    // The documents (or images) in some folders, every page of each.
    async files(folderIds, src) {
      const out = [];
      for (const fid of folderIds) {
        for (let page = 1; page < 500; page++) {
          const r = await http('/Admin/DocumentCenter/Folder/Document_AjaxBinding?renderMode=0&loadSource=' + src + (src === 4 ? '&root=1' : ''), {
            method: 'POST', headers: DJ,
            body: JSON.stringify({folderId: Number(fid), getDocuments: 1, imageRepo: src === 4, renderMode: 0, loadSource: src,
                                  requestingModuleID: 75, searchString: '', pageNumber: page, rowsPerPage: 100,
                                  sortColumn: 'LastModifiedDate', sortOrder: 1})});
          if (!r.ok) throw new Error('document list did not load (HTTP ' + r.status + ')');
          const j = JSON.parse(r.text);
          const got = j.Documents || [];
          got.forEach(x => out.push({id: x.ID, folder: Number(fid), name: x.DisplayName || x.Name || '', file: x.Name || '',
                                     type: x.FileType || '', status: x.StringStatus || '', link: x.CopyLink || '',
                                     refs: x.ReferenceCount || 0, modified: x.LastModifiedDateString || '', archived: !!x.IsArchived}));
          if (got.length < 100 || out.length >= (j.TotalCount || 0)) break;
        }
      }
      return out;
    },
  };

  // --------------------------------- Document Center / Public Images: actions
  // Folders are the categories, documents and images the items. The calls are
  // the Document Center page's own: Folder/{Publish|Unpublish|Delete}/{id}
  // (a folder is deleted with what's in it), and
  // Document/{Publish|Unpublish|Delete}/{first id}?folderID= with every ticked
  // id as listIDs[] (its bulk actions). The two top folders are never changed.
  docs._src = {};
  docs.cats = async function () {
    const out = [];
    for (const r of await docs.roots()) {
      docs._src[r.id] = r.src;
      out.push({id: r.id, name: r.name, parent: 0, published: null, top: true});
      for (const f of await docs.folders(r.id, r.src)) {
        docs._src[f.id] = r.src;
        out.push({id: f.id, name: f.name, parent: f.parent, published: null, root: r.name});
      }
    }
    return out;
  };
  const docStatus = s => /^published$/i.test(clean(s)) ? true : (/draft|unpublished|declined|submitted/i.test(s || '') ? false : null);
  docs.items = async function (cid) {
    const src = docs._src[cid] !== undefined ? docs._src[cid] : 0;
    return (await docs.files([cid], src)).map(x => ({id: x.id, name: x.name || x.file, published: docStatus(x.status),
                                                    status: x.status}));
  };
  const docsForm = ids => {
    const b = new URLSearchParams();
    ids.forEach(i => b.append('listIDs[]', i));
    return b.toString();
  };
  const DOC_VERB = {publish: 'Publish', unpublish: 'Unpublish', delete: 'Delete'};
  docs.itemAct = async function (action, cid, ids) {
    const res = result();
    const verb = DOC_VERB[action];
    if (!verb) { failAll(res, ids, 'not available for documents'); return res; }
    const H = Object.assign({'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}, XHR);
    for (let i = 0; i < ids.length; i += 100) {
      const part = ids.slice(i, i + 100);
      try {
        const r = await post('/Admin/DocumentCenter/Document/' + verb + '/' + part[0] + '?folderID=' + cid, docsForm(part), H);
        if (r.ok) res.ok.push(...part); else failAll(res, part, 'HTTP ' + r.status);
      } catch (e) { failAll(res, part, String(e.message || e)); }
    }
    return res;
  };
  docs.catAct = async function (action, ids) {
    const res = result();
    const verb = DOC_VERB[action];
    const H = Object.assign({'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}, XHR);
    for (const id of ids) {
      if (Number(id) === 1 || docs._src[id] !== undefined && (await docs.roots()).some(r => r.id === Number(id))) {
        res.failed[id] = 'a top folder (Document Center, Public Images) is never changed'; continue;
      }
      try {
        const r = await post('/Admin/DocumentCenter/Folder/' + verb + '/' + id, '', H);
        let j = null; try { j = JSON.parse(r.text); } catch (e) {}
        if (r.ok && (!j || j.Successful !== false)) res.ok.push(id);
        else res.failed[id] = (j && (j.Message || j.ErrorMessage)) || 'HTTP ' + r.status;
      } catch (e) { res.failed[id] = String(e.message || e); }
    }
    return res;
  };

  // Does each site URL work? (Outside sites are checked by the tool itself:
  // the browser won't read other sites' answers.) A site 404 comes back as
  // the Custom404 page with a 200, so that counts as not working.
  async function checkUrls(urls) {
    const out = {};
    let next = 0;
    const worker = async () => {
      while (next < urls.length) {
        const u = urls[next++];
        try {
          const r = await fetch(u, {credentials: 'same-origin', cache: 'no-store'});
          let ok = r.ok, why = r.ok ? '' : 'HTTP ' + r.status;
          const type = r.headers.get('content-type') || '';
          if (r.ok && /html/i.test(type)) {
            const t = await r.text();
            if (/<title>\s*Custom404/i.test(t)) { ok = false; why = 'page not found'; }
          } else { try { await r.body.cancel(); } catch (e) {} }
          out[u] = {ok, why, final: r.url};
        } catch (e) { out[u] = {ok: false, why: 'no answer (' + String(e.message || e) + ')'}; }
      }
    };
    await Promise.all(Array.from({length: Math.min(6, urls.length)}, worker));
    return out;
  }

  const MODS = {bids, faq, news, archive, calendar, forms: formcenter, facilities, resdir, staff, pages, docs,
                links: {check: checkUrls}};
  window.__acRelay = {
    version: VERSION, mods: MODS,
    async call(mod, op, args) {
      const m = MODS[mod];
      if (!m || typeof m[op] !== 'function') throw new Error('no ' + op + ' for ' + mod);
      return m[op].apply(m, args || []);
    },
    _test: {clean, bulkForm, classicBulk, chunks},
  };
})();
