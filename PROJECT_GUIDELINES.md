# AutoCentral: Project Guidelines and Architecture

Status: draft for review, 2026-11-16, updated with the answers to the first
round of questions. It is based on a read-only walk through all 10 modules on
cp-training19 (code revision 5.0.94239). Training18 and training20 have not
been checked yet.

How the tool will be used: we build the whole customer site on our side, review
and QA it, then hand it to the customer. The tool runs on the training sites and
on those customer sites before handoff. They are the same platform, so
everything here applies to both.

## 1. Goal

One tool that can do on a CivicPlus Central site what a person does by hand, for
each module, through a window (GUI) and a list of commands. Selenium does the
clicking. Every change is shown as a dry run first.

Examples of what it should eventually handle:

- "Show me everything in every module, who created it and when."
- "Delete everything in the News Flash and the FAQs."
- "Unpublish every bid created by Z between X and Y."
- "Import these calendars and events, then link them onto these pages."

## 2. Scope

### Modules, first round

| Module | Admin page | Style | History Log module id |
|---|---|---|---|
| Agenda Center | `/Admin/AgendaCenter` | Newer | 65 |
| Archive Center | `/admin/Archive.aspx` | Classic | 15 |
| Bids | `/admin/Bids.aspx` | Classic | 16 |
| Calendar | `/admin/Calendar.aspx` | Classic, month-based | 58 |
| Facilities | `/Admin/Facilities` | Newer | 79 |
| FAQs | `/admin/FAQ.aspx` | Classic | 25 |
| Form Center (not Forms) | `/Admin/FormCenter` | Newer | 71 |
| Resource Directory (also Business / Community Resource Directory) | `/admin/BusinessDirectoryii.aspx` | Classic, flat A–Z list | 12 |
| News Flash | `/admin/CivicAlerts.aspx` | Classic | 1 |
| Staff Directory | `/admin/Directory.aspx` | Classic, tree | 4 |

**Modules are found by URL, never by menu name.**
- Menu names change from site to site. One site has "Business Directory",
  another has "Resource Directory".
- The end of each module's URL is the same on every Central site that has the
  module.
- So the tool goes straight to the URLs in this table, and never navigates by
  menu text.

### Out of scope for now

Pages, Document Center, Photo Gallery, Jobs, Forms (the newer product), site
settings, users and permissions. Linking content onto pages comes after the
first round, because it needs the Pages module.

### Order of work

1. **Read**: inventory every module (this is the first deliverable).
2. **Publish / unpublish**, per module.
3. **Delete**: items, then categories, then "empty the whole module".
4. **Import**: through the site's own Content Import where it can, by clicking
   where it can't.
5. **Cross-module** work, such as linking content onto pages.

- Each phase is done for all 10 modules before the next phase starts. The
  shared code then gets built once, and the module quirks show up early.
- Within a phase, the order of the modules doesn't matter. All 10 have to work
  before the project is done.

## 3. Safety rules (non-negotiable)

These carry over from the Agenda Center tool and apply to every module.

1. **Never notify anyone.**
   - Any Notify Me / subscriber pop-up is answered with **Don't Send**. If
     that button isn't found, click Cancel and stop the run.
   - Never click Send, "Save and Send", or anything that emails users or
     subscribers.
   - The browser layer refuses these clicks by itself, so no module can
     forget.
2. **Dry run first, always.**
   - A live run needs a typed confirmation: `yes`, or `DELETE` for anything
     that deletes.
   - The confirmation shows the site address. The same rules apply on
     training sites and customer sites.
3. **Deletion is permanent.**
   - The site has no recycle bin.
   - The dry run lists every item that would be deleted.
   - `results.csv` keeps a record of what was deleted, including names and
     IDs.
4. **Reading never changes anything.**
   - Reads use background requests or hidden frames only.
   - Reads never open classic edit pages, which can lock an item (History Log
     has an "Unlock" action for this reason).
   - Reads never press Save.
5. **Every change is checked.** Click the real control, then read the site again
   to confirm it happened. If it didn't, report it as an error rather than
   success.
6. **The user logs in**, including 2FA. The tool never handles passwords.
7. **New features are proven on training sites** (cp-training18, 19 and 20)
   before they are used on a customer site.
8. **Stop on repeated failure.** Three failures in a row on one module, or an
   unexpected page or error page that doesn't clear after retries, ends the
   run.
9. **The Stop button** ends a run after the current action, never halfway
   through a click sequence.

## 4. What we learned about the site

### Two kinds of admin pages

- **Newer pages** (Agenda Center, Form Center, Facilities):
  - Lists come from URLs like `Category/Detail/N` and
    `Item/ItemList?categoryID=X&page=1`.
  - Background `fetch()` reads them directly. This is fast: well under a
    second a page.
  - The page structure is similar between modules, but the columns differ.
    Form Center: Name / Submissions / Last Modified / Status. Facilities:
    Name / Upcoming Reservations.
- **Classic `.aspx` pages** (the other seven):
  - `fetch()` is redirected to `Classic.aspx?fromURL=…`.
  - A hidden frame on the same site loads the real page, in about 2.3 seconds.
    Several frames can load at once.
  - They share one design:
    - Categories in a **Published** table and an **Unpublished** table.
    - A "Choose an Action" dropdown per category: View List, Properties,
      Publish/Unpublish, Archive. The option values vary (`viewlist`, `List`,
      `list`).
    - Every action is a post of a hidden form to the module page. The form
      has a page/action name (`strPage`, e.g. `BidItemList`,
      `BidCategoryPublish`, `BidCategoryDelete`), the IDs, `ysnSave`, and a
      `__RequestVerificationToken`. Form names seen: `frmBidCategory`,
      `frmArchiveMasterList`, `frmFAQTopicList`, `frmAlertDetail`,
      `frmBusinessDirectoryDetail`.
    - Item lists have the same Published/Unpublished tables, and "Publish /
      Unpublish / Delete / Archive / Submit / Reject Marked" buttons for
      ticked rows.
    - **Delete is only offered once something is unpublished.**

### Module quirks

- **Calendar:** events are listed one month at a time
  (`/admin/calendar.aspx?CID=N`), so reading a calendar means walking the
  months. Content Import doesn't cover it.
- **Resource Directory:** one flat A–Z list with Marked buttons and no
  category level to speak of. Content Import doesn't cover it.
- **Staff Directory:**
  - A department tree, read and changed through its classic admin page
    (`/admin/Directory.aspx`).
  - Live Edit is not used. The user logs in the normal way, as for every other
    module.
  - The earlier Live Edit script isn't carried over. At most, its
    tree-walking logic may be reused.
- **Agenda Center:** fully handled by the existing AutoCentralAgendaCenter
  tool (agendas, items, default items, folders, workflows, uploads, folder
  scan).

### Who created what, and when

The module lists mostly don't say.

| Module | What the list shows |
|---|---|
| Archive Center | "Last Modified By" only |
| Form Center | "Last Modified" only |
| Agenda Center | "Updated" only |
| Bids, FAQs, News Flash, … | Neither |

**History Log** (`/Admin/HistoryLog.aspx`) fills the gap:

- **What a row has:** title, date and time to the second, the user's name,
  the module's own IDs (e.g. `CID 68 AID 1931`, `AMID 38 ADID 4400`), and the
  action.
- **Filters:** module, action (Create, Modify, Publish, Unpublish, Delete,
  Archive, Approve, …) and date/time range.
- **Pages and history:** 100 rows a page, going back to 2015 on training19.

The tool reads the **Create** rows for a module and joins them to the inventory
by ID. That gives "created by" and "created on" for every module from one
reader.

Content brought in with the site's Content Import shows as created by whoever
was logged in when the import ran. "Created by" for those items is the person
who ran the import, not the original author. Any item with no Create row at
all shows **"unknown"**, never a guess. The **Modify** rows give
"last changed by" for all modules as a bonus.

### The site's own importer

**Content Import** (`/Admin/ContentImport`) takes a zip with a spreadsheet and
the files. It covers:

- Agenda Center, Archive Center, Bids, Facilities, FAQs, Form Center, News
  Flash and Staff Directory (8 of our 10);
- plus Document Center, Image Repository, Jobs, Pages and Photo Gallery.

Imports should build that zip and hand it over, rather than clicking each
record in. Only Calendar and the Resource Directory need click-by-click
imports.

### Other site-wide pages worth using

- **Expiring Items**: a possible extra filter.
- **Site Report** (`ContentReport`): a possible cross-check for counts.

## 5. Architecture

```
            GUI (Tkinter)            Text menu / command line
                  \                        /
                   +------ Plan runner ----+     dry run vs live, steps, undo, timing
                              |
                    Module registry + site profile
                              |
      +-----------+-----------+------------+---------------+
      |           |           |            |               |
  NewerAdapter  ClassicAdapter  CalendarAdapter  ResDirAdapter  StaffAdapter
  (Agenda,      (Archive, Bids,  (classic +      (classic,      (tree,
   Forms,        FAQ, News)       months)         A–Z)           Live Edit)
   Facilities)
      |                 |
   FetchReader     FrameReader (hidden iframes, N at a time)
      \                 /
       Browser session: login hand-off, never-notify guard, dialogs,
       error-page retry, Stop
                              |
                     HistoryLogReader (created by / on)
```

### Layers

1. **Browser session** (reused from the Agenda Center tool's `browser.py`):
   - attaches to Chrome after the user logs in;
   - handles dialogs, the never-notify guard, error-page retries, and Stop.
2. **Readers.**
   - `FetchReader`: `fetch()` plus HTML parsing inside the browser, for the
     newer pages.
   - `FrameReader`: hidden same-site iframes, for classic pages. It runs
     several at once, waits for the page to settle, returns the HTML, and
     removes the frame.
3. **Site profile.**
   - On connect, check each module's fixed admin URL (the table in section 2)
     to see which modules the site has. Record the site's revision.
   - The admin menu is read only to pick up each module's display name, for
     the screen. It is never used to find or reach a module.
   - A module missing on a site is data, not code. Saved per site in
     `profiles\<site>.json`.
4. **Module adapters.** They share one interface:

   ```
   categories()                      -> list[Category]
   items(category)                   -> list[Item]
   publish / unpublish(items|cats)   -> results
   delete(items|cats)                -> results   (unpublishes first when the site requires it;
                                                   item deletes take include_archived, off by default;
                                                   category deletes always remove archived items too)
   purge()                           -> everything in the module, in the right order
   import_(source)                   -> results   (Content Import zip, or clicks)
   ```

   - Each module is mostly a **settings block**: its URLs, form name,
     `strPage` values, table selectors, option values and History Log id.
     There is no new code per module unless it has a real quirk.
   - The Agenda Center adapter wraps the existing tool's `site.py` rather
     than rewriting it.
5. **History Log reader.** For each module, it searches Create (and Modify)
   rows by date range and pages through them. It returns `{ids: (who, when)}`
   for the join, and caches per site so a second scan is quick.
6. **Inventory.** One row per category or item, in a single model shared by all
   modules:

   | Column | Notes |
   |---|---|
   | site, module | |
   | level | category / item |
   | category, name | |
   | status | published / unpublished / draft / archived … |
   | created_on, created_by | from History Log; "unknown" if not found |
   | modified_on, modified_by | list or History Log |
   | ids | the site's own IDs, e.g. `AMID 38 ADID 4400` |
   | url | admin link |
   | extra | per-module fields: meeting date, event date, submissions … |

7. **Filters.** One filter model for every module: module, category, status,
   created/modified date and time range, created/modified by, name contains
   or regex, IDs, limit. Module-specific filters (meeting date, event date)
   are added on top.
8. **Plan runner** (reused):
   - steps across modules, dry run versus live, typed confirmation;
   - `results.csv`, `undo.json` and `timing.txt` per run;
   - Stop, and stopping after repeated failures.
9. **Front ends.**
   - **GUI:**
     - a Scan tab that fills one big sortable, filterable table for all
       modules;
     - a tab per module for actions;
     - a Plan tab;
     - export to `.xlsx`.
   - **Text menu and command line** for the same actions, e.g.
     `run_menu.bat --scan-all` or `--plan plans\x.json`.
   - The core never prompts or prints. It reports through callbacks, as
     today.

### Speed

- **Reads:**
  - Newer pages: by background request.
  - Classic pages: several hidden frames at once. Start at 4 and tune it.
  - History Log: one search per module instead of one per item.
  - Target: a full 10-module inventory of a training site in a few minutes.
- **Changes:**
  - Use the site's bulk "Marked" buttons wherever they exist (one click for
    many items) instead of acting on items one by one.
  - Posting the classic hidden form directly would be faster than clicking,
    but it skips the page's own checks. Use real clicks first. Only consider
    direct posts after they've been proven on training sites, and never for
    anything that could notify.
- **Imports:** Content Import zips wherever they're supported.

## 6. Repository and conventions

- **New repo:** `AutoCentral` (private, under DavidHazelwoodCivicPlus). The
  user pushes, since this machine has no git credentials.
- **The Agenda Center tool stays as it is** and keeps working. Its code is
  copied or imported into the new repo as the Agenda Center adapter. Nothing
  in `Desktop\Tools\AutoCentralAgendaCenter` changes because of this project.
- **Running:**
  - Python 3.10+, Selenium 4, openpyxl.
  - `run.bat` / `run_menu.bat`: no `.exe`, because the firewall flags it.
  - A private `.venv` that reinstalls itself when `requirements.txt`
    changes.
- **Layout:**

  ```
  autocentral/
    browser.py        session, never-notify guard, dialogs, retries (from the Agenda Center tool)
    readers.py        FetchReader, FrameReader
    profile.py        site profile: modules, names, URLs, revision
    historylog.py     History Log search + join
    models.py         Category, Item, InventoryRow, Result
    filters.py        shared filter model
    modules/
      base.py         adapter interface
      newer.py        newer-page adapter
      classic.py      classic-page adapter
      settings.py     per-module settings blocks
      agenda.py       wraps the Agenda Center tool
      calendar.py     month walking
      resdir.py       A–Z list
      staff.py        department tree (classic admin page)
    contentimport.py  build and submit Content Import zips
    plan.py           steps, runner, undo, timing
    reporting.py      callbacks, results.csv, undo.json, timing.txt
    inventory.py      table + xlsx export
    cli.py, gui.py
  profiles/           saved site profiles (not in git)
  runs/               per-run results (not in git)
  tests/              offline tests on saved pages
  ```

- **Tests:**
  - Each adapter gets offline tests that run against saved copies of real
    pages. The exploration already saved many in the scratchpad.
  - Live tests run only on training sites, as a checklist per module:
    1. scan;
    2. dry run;
    3. live on a few test items;
    4. re-scan to confirm.
- **Documentation:** the README is written in plain language for the people who
  run it, as with the Agenda Center tool. This file holds design decisions.

## 7. A module is "done" for a phase when

1. It is found by the site profile on training18, 19 and 20, or recorded as
   missing there.
2. Its offline tests pass on saved pages from at least two sites.
3. A dry run on a training site lists exactly what a person sees in the admin
   page.
4. The live run matches the dry run. A re-scan confirms the result, and
   `results.csv` records it.
5. The README says what it can and can't do.

## 8. First deliverable: read-only inventory

- **Site profile:** connect to a training site and build its profile.
- **Scan all 10 modules:** read every category and item through the adapters,
  without changing anything.
- **Join the History Log:** add created by / created on and modified by /
  modified on.
- **Show and export:** show everything in one GUI table with filters, and
  export to `.xlsx`.
- **Speed:** report how long each module took.
- **Gaps:** report anything the tool couldn't read (pages that failed, items
  without a Create row) instead of hiding it.

No publish, unpublish or delete buttons exist until this is right on all three
training sites.

**Status (2026-11-16):** built and run on training19.

| Area | State |
|---|---|
| Module lists | All 10 modules read |
| Item counts | Match the sites' own counts, every category |
| Resource Directory | Paged, all 403 resources |
| Calendar | Every event, past ones included; display options put back afterwards |
| Staff Directory | Departments from the tree, no Live Edit |
| History Log | Join and per-site cache built |

Still to do in this phase:

- archived records (Show Archived, which is mostly version history);
- employees, when a site has staff (Export Staff gives no IDs);
- training18 and training20.

**What we learned while building:**

- **First History Log row, not just Create.** Archive Center logs new items
  as "Save And Publish", not "Create". So "created" comes from a record's
  first History Log row, as long as that row is a create/save type.
- **Imports leave no rows.** FAQs imported with the FAQ module's own Import,
  and the Form Center's sample forms, have no History Log rows at all. They
  show as unknown.
- **Item IDs must match the category.** History Log IDs need both numbers
  matched: "CID 2" (a category) must never be read as form 2.

## 9. Open questions

None right now.

### Answered

| Question | Answer |
|---|---|
| Module names | They differ in the menu, but the URL endings are the same on every site. Target URLs. |
| Customer sites vs. training sites | They are the same platform. |
| Production use | Yes. Customer sites are built and QA'd on our side, then handed over. The same rules apply. |
| Staff Directory | No Live Edit. Normal login, classic admin page. |
| Module order | Doesn't matter. All 10 have to be finished. |
| Archived items | Item deletes get an "include archived items" option (off by default). Deleting a category always deletes its archived items too. |
| Content Import and History Log | Imported records show as created by whoever was logged in when the import ran. |
