# AutoCentral

Reads every module of a CivicPlus Central site into one table: each
category and item, its status, and who created it and when.

**Every module:** publish, unpublish and delete the ticked rows (a ticked
category means the whole category), delete everything in a module, create
categories from a list, and undo a live run. Every action runs as a dry run
first; Notify Me is always answered Don't Send. See **Module actions** below.

## Running it

- **`run.bat`** opens the window.
- **`scan.bat`** scans from the command line and saves a spreadsheet.

The first time, either one creates a private Python environment and
installs `selenium`, `openpyxl` and `beautifulsoup4`. It installs again by
itself whenever `requirements.txt` changes. You need Python 3.10 or newer, and
Chrome.

### The window

The window has an **Overview** tab and one tab per module. Each tab is named
the way the site names that module (e.g. "Bid Postings").

1. Enter the site URL and click **Open Chrome and log in**. Log in yourself,
   including 2FA. The status line says when the tool has taken over.
   Whatever was last read on that site shows straight away, with the date
   and time it was read.
2. Read the site, all at once or one module at a time:
   - **Read all modules** (top right) reads every module except Pages and
     Document Center, which can take minutes: read those from their own tabs.

   The tabs are Overview, Pages, Document Center, then the modules. A tab is
   named the way the site names its module ("Bid Postings"), except Pages,
   Document Center and Resource Directory, which always use those names.

   While the tool works, the top bar shows what it's doing with dots that
   blink (. .. ...) and **about m:ss left**. The first guess comes from how
   long the same read took last time; once a read reports its progress (pages,
   folders, links), the countdown follows its real speed. With no way to guess
   yet, it shows the time so far.
   - **Read this module** (in a module's tab) reads only that one.

   Reading never changes anything. **Stop** ends a read after the pages being
   read. Leave **Created info (History Log)** ticked to fill in Created and
   Created By (see below).
3. A module's tab shows its categories with their items under them, as a
   tree. The Staff Directory nests departments the way the site does.
   Besides Status, Created, Created By, Last Modified and Modified By, each
   module has its own columns, for example:

   | Module | Its own columns |
   |---|---|
   | Agenda Center | Meeting date, Minutes |
   | Calendar | Events (calendars); Starts, Ends, Past (events) |
   | News Flash | Start Date, End Date |
   | Archive Center | Date |
   | Form Center | Submissions |
   | Facilities | Upcoming reservations, Subfacilities |

   Click a column heading to sort; items sort within their category.
4. Filter a tab:
   - by **Status**;
   - by **Created by**. Pick **(no info)** to see only the records the site has
     no creator for;
   - by **Created from / to**: `2024-03-05`, or `2024-03-05 14:00` for a time.
     **Also show records with no created info** is on by default, so a date
     filter never hides those records; untick it to see only records with a
     known date;
   - by **Search** text, in the name, category, notes and the module's own
     columns, with an optional regex.

   A category whose items match but which doesn't match itself stays in the
   tree, in grey.
5. Tick rows with the box in front of the name, or select rows and press the
   space bar. Ticking a category ticks the rows shown under it. **Tick shown**,
   **Untick all** and **Ticked only** help with bigger selections. The action
   bar above the table acts on the ticked rows (see **Module actions**).
6. The **Overview** tab lists every module: on the site or not, counts, how
   many have created info, when it was last read, and the notes from the
   read (pages that failed, records with no History Log rows). Double-click a
   module to open its tab.
7. Export:
   - **Export shown rows…** in a module tab saves what that tab shows.
   - **Export everything…** in the Overview saves every module.

   Both go to an `.xlsx` with three sheets:
   - **Inventory**: the rows, with each module's own columns at the end.
   - **Summary**: per module, the counts, the time taken, and the notes.
   - **ScanInfo**: the site and the time of the export.

The last read of each module is kept in `cache\<site>\inventory_<module>.json`.

### The Pages tab

Every page on the site, published or not, nested the way the site nests them,
with each page's status in the Status column: published, draft …, or
**linked page** for a menu item that goes straight to another page, a file or
an outside site (Live Edit's "This page links to another page or website").

- **Page rows** show:
  - the page's address and its kind (page, layout page, custom links, or the
    linked page's target type);
  - **Links to:** where a linked page goes;
  - its parent pages, and the module widgets on it (e.g. "Calendar (November 2026)");
  - how many links of each kind it has: page links, module links, files, image
    files, external links, other site links, email;
  - after **Check links…**, how many of them are broken.

  The Detail column names the outside sites it links to. Phone numbers aren't listed.
- **Under each page**, one row per widget, link and image in the page's own
  content. The site's header, menus and footer are left out, so every page
  doesn't repeat the same links. Each row has:
  - **URL:** where it points;
  - **Shown as:** the link text, or the image's alt text;
  - **Internal / external**;
  - for files and images, the **File name** (the real one from Document
    Center / Public Images when that tab has been read), and **In Document
    Center**: which folder it's in, or "not found";
  - **Works:** after **Check links…**, yes or why not.
- **Text blocks:** each editor (text) block on a page is its own row,
  named by its first line, with its word count. A block that shows on
  several pages (a shared or inherited area) says "Shared: on N pages".
- **The preview panel** under the table shows the selected text block,
  or all of a selected page's text blocks, in two tabs:
  - **Text:** the readable text;
  - **HTML:** the block's actual HTML.

  **Copy HTML** copies it. **Preview in browser** opens the block(s) in your
  browser with the site's own stylesheets, close to how they look live (menus,
  other widgets and the layout are left out; double-click the page to see the
  real thing). Drag the bar between the table and the panel to resize them.
  The Search box looks inside text blocks too, so you can find every page that
  says something, or uses a certain bit of HTML. Exports include each block's
  text and HTML (cut at Excel's 32,767-character cell limit).
- **Show:** one tick box per status and kind of row, with counts. Untick
  `widget`, `image` or `site link` to hide those rows. Untick `draft` to hide
  draft pages and everything on them. **All** / **None** set them all.
- **Double-click** any row with an address (a page, link, file or image) to
  open it in your own browser, not the tool's Chrome, so a read in progress
  isn't disturbed. Drafts show there when you're logged in to the site with
  Live Edit on.
- **File types:** a second row of tick boxes, one per file type found (pdf,
  png, docx …) with counts. Untick a type to hide those files and images.
- **Check links…** checks every distinct address once:
  - **Site addresses** are checked from inside the logged-in Chrome, so drafts
    and admin-only files answer as they do for you. A Central page that shows
    its "page not found" page counts as broken.
  - **Outside addresses** are checked by the tool itself, 16 at a time. A site
    that blocks automated checks shows as "no: HTTP 403 (the site may block
    automated checks)". On training19 it checked 1,787 distinct addresses (1,175 on
    the site, 612 outside) in 109 s.
- **Export site map…** saves the shown pages as a CivicPlus-style site map in
  Excel: Home in column A, its sub-pages in column B, theirs in C, in menu
  order. Each page's status, kind, address (a link), linked target, widgets and
  link counts follow. Filters and Show apply, so you can export just the
  published pages, for example.
- **Export shown rows…** gives every row as a spreadsheet: which pages link to
  which file or address, for relinking.
- **Columns:** drag the Name column as wide or narrow as you like; it keeps
  that width.

### The Plan tab: queue changes, then let them run

Build up a list of changes from the other tabs, check it, then let it run on
its own:

- **Add ticked to plan ▾** in every module tab (Publish / Unpublish / Delete)
  queues that action for the ticked rows instead of running it now.
- **Move ticked pages…** in the Pages tab: choose the new parent page from a
  list you can filter. Pages can't be moved under themselves or their own
  sub-pages, and the home page and layout pages aren't moved.
- **Move ticked…** in the Staff Directory tab: ticked departments go under
  the department you choose (with everything in them); ticked employees move
  to it.
- **Find and replace…** in the Pages tab:
  - type what to find and what to put instead;
  - choose all read pages, the pages shown in the table, or ticked pages;
  - options: match case, whole words, regular expression;
  - normally only the text visitors read changes, never links, image
    addresses or tags; tick **Search the HTML** to change those too (for
    relinking, for example).

  Every match is listed with its text before and after; untick any to leave
  alone, then **Add ticked to the plan**.

The **Plan** tab lists the steps in order. Move steps up or down, remove them,
or save the plan and open it again later. It's also kept by itself between
sessions. **Run the plan…** works like every other action: a dry run of every
step first, then **Run for real…** with the site's address typed. There is one
results list, `results.csv` and `undo.json` for the whole run. **Undo a live run…** puts back what
the run changed: moves go back to where they were, text blocks get their old
HTML, published/unpublished flips back. Deletes can't be undone.

**How the text is changed:** the same way a person does it in Live Edit. The
tool opens the page in its own tab with Live Edit on, clicks into the text
block, switches to HTML view, puts the new HTML in, presses the green check
(Done Editing), then Save, then Publish again if the page was published. A
page with unsaved Live Edit changes (a working copy) is left alone and listed.
On training19 that took about 12 s for a page with one changed block. Moves
take a fraction of a second each.

**Stepping through pages:** in the Pages tab, ← and → (or **◀ Previous page** /
**Next page ▶**) go from page to page in the preview panel. **Preview shown pages
in browser** puts every page shown in the table into one preview in your
browser, where ← and → step through them too.

Tested live on training19 (2026-11-20), as one plan:
- a page moved;
- a typo fixed in a published page (clicked through Live Edit and published again);
- an employee moved to another department;
- a demo FAQ question deleted.

It took 19 s, and the end check confirmed each change. Undo moved the page
and the employee back and put the original text back.

### The Document Center tab

Document Center and Public Images, every folder (nested) and every file in it,
with its file name, type, status, address and Central's own reference count.
Once Pages has been read too, each file says **Used on pages** (how many) and
**Pages** (which), and the page rows get the files' real names. A file used
nowhere shows 0. On
training19: 694 folders and 3,739 files in 117 s. The **File types** tick boxes work
here too. Double-click a file to open it in your browser.

**Actions** (2026-11-21): tick files, images or folders, then **Publish ticked…**,
**Unpublish ticked…** or **Delete ticked…** (dry run first, then the site's
address typed). A ticked folder means the whole folder: deleting it deletes
its files and sub-folders too, deepest first. **Delete everything in this
module…** deletes every folder in Document Center and Public Images. The two
top folders themselves are never changed. These are the Document Center page's
own requests (`Document/Publish|Unpublish|Delete` with the ticked IDs, one
request per folder; `Folder/Publish|Unpublish|Delete/{id}`). Publishing
answers Notify Me with Don't Send.

### Wipe the whole site

**Wipe the whole site…** on the Overview tab empties a test site before a
migration. It runs each module's own **Delete everything**: Agenda Center, Bid
Postings, FAQs, News Flash, Archive Center, Calendar, Form Center, Parks and
Facilities, Resource Directory and Staff Directory. Then every page except
Home and the layout pages (sub-pages first), then every folder, document and
image. It asks first, runs a dry run that lists everything, and the live run
needs the site's address typed. A part that fails is reported and the rest
go on (`actions\wipe.py`). Dry run on training19 (4 modules): 728 folders,
361 entries, 7 categories listed in under a minute.

**How it reads the pages.** Live Edit's own page list gives every page in one
request. Each page is then read as you see it while logged in. Unpublished
pages only show with Live Edit on, so the tool turns the Live Edit and
"Show All Items" settings on for those reads and puts them back as they were.
Published pages are read without Live Edit, which is about four times
quicker. The tool picks the quicker way batch by batch. History Log isn't read
for Pages: it is far too big to be worth it.

**Speed:** on training19, 882 pages (803 of them drafts), with 2,977 widgets,
links and images, took about 7.5 minutes (463 s). Central answers one logged-in
request at a time, so reading pages in parallel only helps a little. A site
whose pages are mostly published reads in about a quarter of that time.

### Module actions

Every module tab has an actions bar:

| Button | What it does |
|---|---|
| **Publish ticked…** | Publishes the ticked items. A ticked category is published with every unpublished item in it. Notify Me is answered **Don't Send**. |
| **Unpublish ticked…** | Unpublishes the ticked items and categories. A ticked category is unpublished itself; that hides everything in it. |
| **Delete ticked…** | Deletes the ticked items, unpublishing them first. A ticked category is deleted with everything in it, because the site won't delete a category that still has content. In Staff Directory that includes the departments inside it. |
| **Delete everything in this module…** | Every category with everything in it. In the Resource Directory: every entry. |
| **Create categories…** | Names one per line, published or not. Names already on the site are skipped. Calendar names are limited to 50 characters. Not in the Resource Directory, which has no categories here. |
| **Undo a live run…** | Picks a live run's `undo.json` and reverses what it changed: it republishes what was unpublished, unpublishes what was published, and deletes the categories it created. Deletes can't be undone. |

**Every action runs as a dry run first.** A run window lists what would
happen and changes nothing. **Run for real…** unlocks after the dry run, and
asks you to type the site's address. Every run keeps `results.csv` and
`undo.json` in `runs\<site>\<time>_dryrun` or `_live`. After a live run,
that module is read again by itself.

**Nothing ever notifies subscribers.** Every publish sends exactly what the
Notify Me pop-up's **Don't Send** button sends (`ysnSend`, `ysnSendAtStart`
and `ysnSendBeforeDays` all false). Clicks still go through `safe_click`,
which refuses any Send control.

**Speed.** The tool works the way the WebCentral Relay extension does. Each
request is sent from inside the logged-in page (`autocentral/actions/relay.js`),
with every ID in one request where the site's bulk forms take them (up to 150
at a time). Requests go one at a time, because Central handles them one at a
time and parallel requests only slowed it down. Nothing is re-read after each
request. One read at the end checks the result and lists anything still there
as an error row.

Measured on training19 (2026-11-20), live, including the dry-run reads and
the final check:

| Module | Delete everything | Smaller jobs |
|---|---|---|
| Bids | 1 category: 6 s | publish 2 bids: 5 s |
| News Flash | 3 categories, 3 items: 7 s | |
| Archive Center | 2 archives, 15 items: 9 s | |
| Form Center | 3 categories, 41 forms: 8 s | publish 2 forms: 2 s |
| Facilities | 3 categories, 26 facilities: 23 s + 7 s (the first run left 2 facilities with sub-facilities; since fixed) | |
| Calendar | 3 calendars, 56 events: 30 s | publish 2 events: 5 s |
| Staff Directory | 11 departments (nested): 15 s | |
| FAQs | 20 topics, 79 questions: 69 s (since then each post reuses the list it read: a topic with 3 questions cleared in 1.9 s instead of 2.3 s) | delete a topic with 3 questions: 6.5 s |
| Resource Directory | 405 entries: 132 s (about 0.3 s an entry, inside Central's own bulk processing) | |
| Agenda Center | not run live (the migration uses its content); dry run of 8 categories with 554 agendas: 32 s | create and delete a category: 3 s + 7 s |

**Per module, what the site allows:**

- **Archive Center:** an item without a document can't be published (the site's own Publish refuses it too).
- **Calendar:** past events are included when clearing a calendar. A recurring event's occurrences are handled one by one.
- **Facilities:** a facility with sub-facilities is deleted after them. A facility with reservations is left in place and named in the results.
- **Form Center:** the built-in **Sample Forms** category is emptied but never deleted (the site doesn't allow it).
- **Staff Directory:** employees are read with their IDs and status, so they're published, unpublished and deleted directly. (A read saved before this change has no IDs; those employees are found in their department by name.)
- **Pages:** Publish publishes the page's current version. Unpublish and Delete are the
  page's own Live Edit actions. Ticking a page ticks the rows under it, its sub-pages
  included. Sub-pages are deleted before their parents, and the site refuses to delete a
  page that still has sub-pages. The home page and the layout pages are never unpublished
  or deleted. A page with an unsaved Live Edit working copy is left for you to publish in
  Live Edit. There is no "Delete everything" or "Create categories" for Pages.
- Archived items, which the site hides behind Show Archived, aren't touched. If one keeps a category from being deleted, the final check says so.

**Agenda Center** has the same bar. Its actions use its own code
(`agendacenter/`):

- Published agendas are unpublished before they are deleted.
- A deleted category takes its agendas, items, default items, item folders and workflows with it.
- Publishing a ticked category also publishes every draft agenda in it.

D10 migrations (JSON exports, downloads, Content Import, the post-import
work) are done with the separate **D10 Agenda Center migration tool**
(`Desktop\Tools\D10AgendaCenterMigration`).

### What the WebCentral Relay extension does, and where it is here

| Extension feature | How the extension does it | In this tool |
|---|---|---|
| Bulk Destroy: unpublish + delete categories, optionally clearing their contents first (Agenda Center, Bids, Calendar, FAQ, Facilities, Form Center, News Flash, Staff Directory) | direct POSTs; Agenda Center content through the page's own bulk functions in a hidden frame | **Delete ticked** / **Delete everything**, same requests. Bulk where the site allows it: one request for many IDs, where the extension makes one per category or per item. Plus Archive Center and Resource Directory, which the extension doesn't cover. |
| Content clearing (items unpublished, then deleted, per category) | re-lists after every step | the same, without re-listing after each step: one check at the end |
| Bulk Create categories from a workbook (published) | Add Category form posted directly | **Create categories…** (names typed in, published or not) for every module with categories, Archive Center included |
| Pick categories from the site, filter, select all | in the extension window | the module tab's ticks, filters, **Tick shown** |
| Name-prefix safeguard, typed DELETE | | dry run first, then the site's address typed |
| Results CSV | downloaded | `results.csv` and `undo.json` per run |
| Jobs and Quick Links | same patterns | not here: they aren't modules of this tool (`PROJECT_GUIDELINES.md`, out of scope for now) |
| Document Center / Public Images folders (destroy by path, documents, reference checks) and the folder importer | folder tree JSON calls | the Document Center tab: publish, unpublish and delete ticked files, images and folders, and delete everything; the folder importer isn't here |

### The D10 project tab (add-on)

An add-on for migrating one D10 site into Central from its JSON exports. It
can be switched off or removed when the project is done:
`"addons": {"d10_project": false}` in `settings.json` hides the tab, and
deleting `autocentral\addons\d10_project\` (plus its line in
`autocentral\addons\__init__.py`) removes it. Nothing else in the tool uses it.

1. **D10 exports folder:** the folder with the `www-*.json` files. Its work
   goes in a `central-project` folder inside it.
2. **Put the pages under:** `Home` for a real site. For a test on a shared
   site, use a page name of its own (it's made first) so nothing mixes with
   the site's pages.
3. **1. Plan** reads the exports, decides where everything goes, downloads
   the files and pictures from the old site and writes the tracking
   workbook. Nothing on any site changes.
4. Open Chrome and log in at the top of the window, then **2. Run all steps
   on the site…** (type the site's address to confirm). Each step is safe to
   run again: what's done is skipped. **Run ticked steps…** runs some of them.

| Step | How |
|---|---|
| Plan | microsites become section pages with their pages under them; the old menu (site map from the team's D10-to-Central Migration Tool, run hidden) orders and groups them. Public notices and press releases go to News Flash, pages with dates to Calendar, directory listings to Staff Directory, ordinances to Archive Center, businesses to Resource Directory |
| Download | every file and picture the content uses (not agenda files: those are the Agenda Center step's) |
| Folders | one top folder in Document Center and one in Public Images, named after the site |
| Files | Content Import (zips under 900 MB) |
| Find files | where each landed: Central names a stored file after its display name, so files are matched by folder and display name |
| Content | Content Import: pages (parents first), news, jobs, staff, archive, with every file link already pointing at the new file |
| Find pages | where each page landed; any under the wrong parent is moved |
| Save and publish pages | Content Import leaves every page locked with unsaved Live Edit changes, so each is saved in Live Edit (Save and Publish if it was published on D10); about 4 s a page |
| Relink | page-to-page links, in Live Edit (about 12 s a page with such links) |
| Staff photos | in each person's own Staff Directory form (Content Import silently leaves out anyone whose row has a photo); about 6 s a person |
| Calendar | the Calendar module's own Import (CSV, from "All Calendars"); calendars made first; its "Import results" prompt is read and answered |
| Resource Directory | its own Import Items popup, clicked like a person (the popup hands the file back to the page that opened it), with the migration tool's file and every picture pointing at the new Image Repository; **never** with "Delete all existing entries" (that popup's default is yes); checked by counting entries before and after |
| Agenda Center | the D10 Agenda Center tool's whole run, unchanged (its copy is in `addons\d10_project\agenda_tool`): read the D10 files, download, batches, empty first (only if ticked, asked again), categories, Content Import, finish the agendas (saves, video links, minutes), publish, check. The checklist shows which part it's on. It first checks the Agenda Center admin page opens (on training19, 2026-11-21, it showed "An error has occurred" and nothing could be finished) |
| Publish everything | staff departments and people, news, archive, calendars and events, Resource Directory entries, and every page read back |
| Check | the site against the D10 files: missing, extra, duplicate (see below) |
| Tracking workbook | `<site>-project-tracking.xlsx`: Steps, Pages (old and new address), Files, Links, Modules, Check, To do |

**Prompts.** The site's alerts and confirms ("36 rows imported successfully",
"...no phone or email... Proceed?") hold the page until answered. Every one is
read, written to the log and answered: OK, except anything about deleting,
removing or replacing, which always gets Cancel (`prompts.py`).

**Check the site against the D10 files** (its own button, read only): for each
module, what the D10 files have that the site doesn't (**missing**), what the
site has that they don't (**extra**), and anything on the site more times than
in the D10 files (**duplicate**: names can repeat in the files themselves, like
19 court dates all called "Municipal Court"). It covers pages, documents and
pictures in the project's folders, staff by department, News Flash (its
archive too), Archive Center, Calendar and the Resource Directory. Results go
to the tracking workbook's **Check** sheet. It also runs at the end of **Run
all**. On Hiawatha it found only the 30 demo entries training19 already had
(95 s).

**One button.** Choose the exports folder, log in at the top of the window,
then **2. Run all steps on the site…**: from planning to the tracking workbook,
nothing else to click. Stopped or logged out? Log in and click it again: it
carries on where it stopped.

What Content Import really wants (found on training19, 2026-11-20; its own
template is out of date): "Parent Folder Name" not "Names";
a Pages column `MainContentContainerWidgets` = `Editor`; Staff
`documentFolderName` not "docuemntFolderName"; Jobs need a Start Date. A
parent folder must be a name used only once on the whole site. Its "failed"
messages for pages aren't to be trusted either way: the tool checks the site.

Left for a person (listed in **To do**): the home page layout, and Central
widgets where D10 had slideshows, embedded staff lists or page cards (they
come across as pictures in a row, a list, or links).
Measured on training19 for Hiawatha (96 pages, 466 files, 56 staff, 36 events,
331 businesses, 102 ordinances and resolutions): plan and downloads about
1 min, files import 76 s, content import 71 s, saving pages about 6 min,
relinking under 2 min, staff photos about 4 min, calendar 11 s, Resource
Directory 70 s, publish-everything 2 min. Agenda Center (205 meetings):
download and import about 4 min; finishing not measured there (see above).

### The command line

```
scan.bat --site https://cp-training19.civicplus.com
scan.bat --site https://cp-training19.civicplus.com --modules bids,faq,news --no-history
```

Module keys: `agenda archive bids calendar facilities faq forms resdir news
staff pages docs` (pages and docs are only read when named). The spreadsheet goes to `inventories\<site>_<time>.xlsx` unless you
give `--out`.

## How each module is found

Menu names differ between sites ("Business Directory" or "Resource
Directory"), but the end of each module's admin URL is the same everywhere.
The tool goes straight to those URLs and checks the page's module number. A
module the site doesn't have is listed as "not on this site". The table uses
the name the site gives the module.

| Module | URL |
|---|---|
| Agenda Center | `/Admin/AgendaCenter` |
| Archive Center | `/admin/Archive.aspx` |
| Bids | `/admin/Bids.aspx` |
| Calendar | `/admin/Calendar.aspx` |
| Facilities | `/Admin/Facilities` |
| FAQs | `/admin/FAQ.aspx` |
| Form Center | `/Admin/FormCenter` |
| Resource Directory | `/admin/BusinessDirectoryii.aspx` |
| News Flash | `/admin/CivicAlerts.aspx` |
| Staff Directory | `/admin/Directory.aspx` |
| Pages | `/Admin/Pages` |
| Document Center (and Public Images) | `/Admin/DocumentCenter` |

## Created and Created By

Most module lists don't show who created something or when. The tool reads
the site's **History Log** for each module (every action, from 2000 to
today) and matches its rows to the table by the site's own IDs, such as
`AMID 38 ADID 4400`. The IDs column shows the match.

- **Created / Created By** come from the record's first History Log row, as
  long as that row is a create or save type action. Archive Center logs new
  items as "Save And Publish", so those count.
- **The record predates the log:** if the first row is something else
  (Modify, Publish …), Created stays blank. The Detail column then says when
  the history starts.
- **Last Modified / Modified By** come from the module's list when it shows
  them. Otherwise they come from the record's latest History Log row.
- **Content Import:** items brought in with it show whoever ran the import.
- **Blank means unknown.** The tool never guesses.

## What it reads, per module

| Module | Categories | Items |
|---|---|---|
| Agenda Center | yes | agendas (meeting date, minutes) |
| Archive Center, Bids, FAQs, News Flash | yes | yes |
| Form Center | yes | forms (submissions, last modified) |
| Facilities | yes | facilities and subfacilities |
| Calendar | calendars | events, past ones included, every page |
| Resource Directory | — (flat list) | resources, every page of 100 |
| Staff Directory | departments, with their parent departments | employees from the department tree (ID, published or not), with title, email and phone from the module's own Export Staff. Shown "Last, First" by default: untick **Last name, First name** in the tab for "First Last" |
| Pages | every page, published or not, nested under its parent page | each page's widgets, links and images (see **The Pages tab**) |
| Document Center | Document Center and Public Images folders, nested | every file, and the pages it's used on |

Archived categories and items, which the site hides behind **Show
Archived**, are not read yet.

**Calendar display options.** The Calendar's event list hides past events
unless **Previous** is ticked in its Display Events options, and the site
saves those options for your login. While reading, the tool ticks Previous
and every status. It then puts back what was there and says so in the log.
If it ever can't put them back, the Summary sheet says so.

**Staff Directory.** The tool opens the department tree in a hidden frame
and runs the page's own **Expand Current View**, so it never needs Live Edit.

## Speed

- **Lists:** the module lists take seconds to about a minute per module.
- **History Log, first scan:** it can take several minutes on a busy module.
  On training19 that was about 6 minutes for Archive Center, which has
  23,000+ History Log rows.
- **History Log, later scans:** what was read is kept in `cache\<site>\`, so
  later scans only read from the last scan's date onward.
- **Starting over:** tick **Re-read the whole History Log** (or use
  `--history-refresh`) to read it all again.

## Layout

```
autocentral/
  browser.py     Selenium session: login hand-off, never-notify guard, background reads
                 (fetch for the newer pages, hidden frames for classic .aspx pages)
  modules.py     the 10 modules: URL, style, module number
  parse.py       page HTML -> data (tested offline on saved pages)
  scanner.py     site profile + one reader per module style
  historylog.py  History Log search + matching to the table
  inventory.py   filters + spreadsheet export
  store.py       last read of each module, saved per site
  agendacenter/  Agenda Center actions (from the AutoCentralAgendaCenter tool):
                 site.py (page actions and in-page requests), plan.py (runner: dry run /
                 live, undo), jobs.py (the jobs the window runs), filters.py, models.py
  actions/       publish / unpublish / delete / create for every module but Agenda Center:
                 relay.js (requests sent from inside the logged-in page, per module),
                 engine.py (runs relay.js), jobs.py (the jobs: dry run, live, final check, undo)
  gui_agenda.py  the Agenda Center actions bar and the run window
  gui_actions.py the actions bar of every other module tab
  gui_plan.py    the Plan tab, Find and replace, Move pages, Move staff
  plan_steps.py  plan steps: run in order (dry run / live), undo
  editing.py     find and replace rules, and the Live Edit page editor
  sitecontent.py Pages and Document Center rows, files matched to pages, the site map export
  linkcheck.py   checks that each link, file and image address works
  models.py      InventoryRow, HistoryEntry, ModuleScan
  cli.py         command line (scan.bat)
  gui.py         the window: Overview + one tab per module (run.bat)
tests/           offline tests:  python -m unittest discover -s tests -t .
tests/pages/     saved copies of training19 admin pages
```
