# Quick start

## 1. Set up (once)

1. Install **Python 3.10 or newer** from python.org. Tick **Add python.exe to PATH**.
2. Install **Google Chrome** if it isn't there.
3. Get the tool: **Code → Download ZIP** on
   https://github.com/DavidHazelwoodCivicPlus/CentralAutomation and unzip it
   (or `git clone` it).

There's nothing else to install. The first start sets up everything it needs.

## 2. Start it

1. Double-click **`run.bat`**. The first start takes a minute or two.
2. Type the site address (e.g. `https://cp-training19.civicplus.com`) and click
   **Open Chrome and log in**.
3. Log in yourself in the Chrome window that opens, including 2FA. The status
   line says when the tool has taken over. Leave that Chrome window open.

## 3. Migrate a D10 Agenda Center

Follow the D10-to-Central Migration Tool's steps. Use this tool where the
migration tool says to work by hand.

1. **Migration tool, steps 1–4:** categories CSV, flagged review, workbook,
   build the zip.
2. **Content Import** in Central: upload the zip. **Each upload must be under
   1 GB.** Bigger sites need several zips, each with its own workbook rows.
3. **Publish the imported agendas:** open the **Agenda Center** tab and click
   **Read this module**. Filter **Status** to Draft, then click **Tick shown**
   and **Publish ticked…**.
4. **Finish what the importer can't (migration tool step 6):**
   **Agenda Center actions → To-do list… → Add from post-import tasks file…**
   Pick the `…-post-import-tasks.json` and the
   `…-agenda-center-attach-after-import` folder. This creates the missing
   meetings (simple, item-based and minutes-only), attaches minutes, adds video
   links, and saves the agendas so their public page gets built.
5. Click **Dry run the list**, check the results, then click **Run for real…**.

## Good to know

- **Every action is a dry run first.** Nothing changes until **Run for real…**.
- **Subscribers are never notified.** Notify Me is always answered
  *Don't Send*.
- **It's safe to run again.** Anything already done is skipped, so after a
  crash or a stop, just run it again.
- **Results** are saved in `runs\<site>\`. **Undo a live run…** can reverse a
  run.
- **What it can't fix:** meetings with no date or no category in D10. Central
  refuses those, and they're listed as errors for a person to decide.
- **Close the tool's Chrome window** when you're done.

More detail is in `README.md`.
