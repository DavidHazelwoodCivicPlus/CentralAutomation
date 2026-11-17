# AutoCentral

Reads every module of a CivicPlus Central site into one table: each
category and item, its status, and who created it and when. Later versions
will add publishing, unpublishing, deleting and importing, module by module
(see `PROJECT_GUIDELINES.md`).

**This version only reads.** It never clicks Save, Publish, Delete or Send,
and it never opens an edit page.

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
   - **Read all modules** (top right) reads every module.
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
   **Untick all** and **Ticked only** help with bigger selections. Ticks don't
   do anything yet. Later actions (delete, publish, unpublish ...) will act on
   them.
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

### Agenda Center actions

The Agenda Center tab has an **Agenda Center actions** bar.

**Every action runs as a dry run first.** A run window lists what would
happen and changes nothing. **Run for real…** unlocks after the dry run, and
asks you to type the site's address. Every run keeps `results.csv` and
`undo.json` in `runs\<site>\<time>_dryrun` or `_live`. After a live run, the
Agenda Center is read again by itself.

**The actions:**

- **Delete ticked / Unpublish ticked / Publish ticked** act on the rows you
  ticked.
  - A ticked category means the whole category. For Delete, that includes
    all its agendas, items, default items, item folders and workflows, which
    the site requires before it deletes a category.
  - Published agendas are unpublished before they are deleted.
  - Publishing always answers Notify Me with **Don't Send**.
- **Compare with import file…** compares the import workbook
  (Master_Content_Import.xlsx) with the Agenda Center as read. Optionally it
  also uses the old site's D10 export (`www-meetings.json`, found by itself
  when it's next to the workbook or in a folder below it).
  - The D10 export fills in meeting dates the workbook lacks. It ties
    complex agendas' items to their meeting by file name. It brings back
    minutes the workbook dropped: complex agendas lose theirs in the
    workbook.
  - Pick the old site's time zone; D10 stores times in UTC.
  - Each meeting in the file is matched to a site agenda by category,
    meeting date and description. The results come in tabs:
    - **Missing**
    - **Missing minutes** (the agenda is there, without minutes)
    - **No category on site**
    - **On site**
    - **Only on site** (on the site, not in the file)
  - **Export comparison…** saves the comparison as a spreadsheet, one sheet
    per tab.
  - **Upload missing…** uploads the missing agendas (simple, minutes-only
    and complex) and adds missing minutes to agendas already there. Its
    options:
    - publish or leave as drafts;
    - create missing categories;
    - download files that aren't in the files folder from the old site
      (they are saved in `<files folder>\downloaded`).

    Drafts can't take minutes on Central, so a draft agenda that needs
    minutes is published for it (Don't Send), then put back to draft.

### To-do list (finishing what the importer can't)

**Agenda Center actions → To-do list…** opens the to-do list. It's the Plan
tab of the old Agenda Center tool: steps that run top to bottom, as one dry
run and then for real. **Save list… / Load list…** keep lists as JSON
files. **Undo a live run…** turns a run's `undo.json` into steps that
reverse it. The list stays between sessions.

**Add from post-import tasks file…** reads the D10-to-Central Migration
Tool's `<site>-agenda-center-post-import-tasks.json` (step 6 of that tool,
"Finish what the importer can't do"). Pick that file, and the folder you
downloaded the files to. The tool adds these steps, in this order:

1. **Create categories** named in the file that aren't on the site.
2. **Create the meetings the importer couldn't** (`create-meeting-manually`:
   meetings that share a category and date with another). Each is built
   from the file's own description: an agenda file with or without minutes,
   items with or without minutes, or minutes only.
3. **Attach minutes** (`attach-minutes`) with Add Minutes. Agendas that
   already have minutes are skipped. A draft agenda is published for it
   (Don't Send) and put back to draft.
4. **Add video links** (`add-media-links`): Link to Media and Additional
   Video Link on the agenda's edit page. Further links have no field on
   Central; they're listed in the results.
5. **Open and save** item-based agendas (`save-to-build-html`). Central
   only builds their public agenda page on a save; until then the link is a
   404. An agenda already saved for its video links isn't saved again.

**How it works:**

- **Finding meetings.** A meeting is found on the site by category, meeting
  date and the description as imported. When only one agenda is on that
  date, the description may differ. Anything that can't be found, or is
  ambiguous, is listed as an error and left alone.
- **Finding files.** Files are looked for in the folder you pick, its
  `<site>-agenda-center-attach-after-import` folder, and anywhere below it.
  With **Download files that aren't in the folder from the old site**, a
  missing file is fetched from the address in the tasks file into
  `<folder>\downloaded`.
- **The CSV version** of the tasks file works too, but it doesn't describe
  the meetings to create by hand, so step 2 needs the JSON.

### The command line

```
scan.bat --site https://cp-training19.civicplus.com
scan.bat --site https://cp-training19.civicplus.com --modules bids,faq,news --no-history
```

Module keys: `agenda archive bids calendar facilities faq forms resdir news
staff`. The spreadsheet goes to `inventories\<site>_<time>.xlsx` unless you
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
| Staff Directory | departments, with their parent departments | employees from the module's own Export Staff (no IDs, so no Created yet) |

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
                 site.py (clicks), plan.py (runner: dry run / live), importdata.py
                 (import workbook), d10.py (D10 export), compare.py, jobs.py,
                 tasks.py (migration tool's post-import tasks -> to-do steps),
                 folderscan.py (workbook from a folder of files)
  gui_agenda.py  the Agenda Center actions bar, compare and run windows
  models.py      InventoryRow, HistoryEntry, ModuleScan
  cli.py         command line (scan.bat)
  gui.py         the window: Overview + one tab per module (run.bat)
tests/           offline tests:  python -m unittest discover -s tests -t .
tests/pages/     saved copies of training19 admin pages
```
