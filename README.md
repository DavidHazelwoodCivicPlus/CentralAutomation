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

1. Enter the site URL and click **Open Chrome and log in**. Log in yourself,
   including 2FA. The status line says when the tool has taken over.
2. Tick the modules to read. Leave **Who created / when (History Log)** ticked
   to fill in Created and Created By (see below; it takes longer).
3. Click **Scan (read-only)**. Each module appears in the table as soon as it
   has been read. **Stop** ends the scan after the pages being read.
4. Filter the table:
   - by module, level (category or item), status, or creator ("unknown" =
     no creator found);
   - by created date range: `2024-03-05`, or `2024-03-05 14:00` for a time;
   - by search text in the name, category or detail, with an optional regex.

   Click a column heading to sort by it.
5. Export the result:
   - **Export shown rows…** saves what the filters show.
   - **Export everything…** saves all rows.

   Both go to an `.xlsx` with three sheets:
   - **Inventory**: the rows.
   - **Summary**: per module, the counts, the time taken, and anything the
     tool couldn't read.
   - **ScanInfo**: the site and the scan time.

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
  models.py      InventoryRow, HistoryEntry, ModuleScan
  cli.py, gui.py front ends
tests/           offline tests:  python -m unittest discover -s tests -t .
tests/pages/     saved copies of training19 admin pages
```
