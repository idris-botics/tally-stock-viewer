# Super Seatings stock viewer: operations runbook

Plain-language guide for running, checking, deploying and recovering the site. Written 2026-10-18.
If something here disagrees with what you see, trust what you see and fix this file.

---

## 1. The big picture (three parts)

| Part | What it is | Where it lives |
|---|---|---|
| **The site** | The web app dealers and admins use (stock, photos, categories, accounts). One small Python program (`serve.py`). | **Render** (service `tally-stock-viewer`, 512 MB, Singapore). It has a 4 GB disk mounted at `data/` that holds the database `mappings.db`, the photos (`data/S.S IMAGE/`) and the stock caches. |
| **The feeder** | A tiny program (`feeder.py`) that reads stock out of **Tally** every few minutes and sends it to the site. No website, no database. | The **shop PC**, as a Windows Scheduled Task. |
| **The backup** | Every night at **02:00 IST** the site copies its database, caches and photos to **Google Drive**. | Runs inside the site (`cloud_backup.py`). |

How stock gets to dealers: Tally -> feeder (shop PC) -> site (Render) -> dealers' phones.
The site never talks to Tally directly. If the feeder stops, the site keeps showing the last stock it received (the "Last updated" time turns red and the System panel's Feeder heartbeat shows stale).

**Auto-deploy:** every push to the `main` branch on GitHub automatically rebuilds and restarts the site. There is no "undo" button for a push other than rolling back (section 7).

---

## 2. Settings (environment variables)

Set on **Render** (Environment tab) for the site, or in the `.env` file on a PC. **Never commit `.env`, never paste tokens into chat or documents.**

| Setting | What it does | Normal value |
|---|---|---|
| `DISABLE_TALLY_SCHEDULING` | `1` = cloud mode: no Tally contact, data arrives from the feeder. | `1` on Render |
| `INTAKE_SYNC_TOKEN` | Secret the feeder must send to push stock. Empty = pushes refused. | secret |
| `INTAKE_MAX_DROP_PERCENT` | A feeder push that would cut the number of cars, or of stock rows, by more than this percentage is refused (see "A push was refused" in section 3). Not checked while fewer than 20 are served. Leave unset normally. | `50` |
| `CLOUD_SYNC_URL` / `CLOUD_SYNC_TOKEN` | **Shop PC only:** where the feeder pushes, and the secret (same value as `INTAKE_SYNC_TOKEN`). | secret |
| `MACHINE_ROLE` | **Shop PC only:** `feeder` = this PC may only run the feeder; every web-app entry point refuses to start. Unset or `full` = normal (Render never sets it). Anything unclear is refused. | `feeder` on the shop PC |
| `FEEDER_TASK_NAME` | Shop PC: name of the Windows task, if it is not `TallyFeeder`. Used by `update_feeder.bat`. | optional |
| `TALLY_URL`, `TALLY_TIMEOUT`, `TALLY_RETRY_ATTEMPTS`, `TALLY_EXPORT_INTERVAL`, `FEEDER_FULL_REFRESH_HOURS` | Shop PC: how to reach Tally and how often to read it (default every 180 s; full re-read every 6 h). | defaults |
| `FEEDER_NEW_ITEM_MIN_MINUTES`, `FEEDER_SLOW_REFRESH_SECONDS` | Shop PC: a new in-stock item missing from the hierarchy triggers a full refresh, at most once per this many minutes (default 10; gap doubles up to 1 h after a failed or slow refresh; slow = over 300 s; a normal refresh takes ~30 s). | defaults |
| `refresh_now.flag` (file, not a setting) | Shop PC: double-click `refresh_feeder_now.bat` to create it; the feeder runs a full refresh within seconds and deletes it. Every refresh is logged in `logseeder.log` as `Full refresh starting: reason=startup/timer/new item/manual` with the Tally export time. | - |
| `FLASK_SECRET_KEY` | Signs login cookies. Changing it logs everyone out. | secret |
| `SESSION_COOKIE_SECURE` | `1` = cookies only over HTTPS. | `1` on Render |
| `SESSION_TIMEOUT_HOURS` | Admin login lifetime (dealers get 90 days). | 8 |
| `SYSTEM_ACCESS_TOKEN` | Secret that pairs a device to the System panel. Empty = panel disabled. | secret |
| `ACCOUNTS_ACCESS_PASSWORD` | First password for the Manage Accounts page (changeable later). | secret |
| `RESCAN_TRIGGER_TOKEN` | Secret allowing a remote "rescan photos" request. Empty = disabled. | secret |
| `GDRIVE_OAUTH_CLIENT_SECRETS_PATH`, `GDRIVE_OAUTH_TOKEN_PATH`, `GDRIVE_BACKUP_FOLDER_ID` | Google Drive backup login and target folder. All three needed, otherwise the backup is off. | set |
| `CLOUD_BACKUP_DAILY_TIME` | When the nightly backup runs (IST). | `02:00` |
| `BACKUP_MAX_DELETIONS` | Overrides the backup's "too many deletions" limit (section 8). Leave unset normally. | unset |
| `DB_PATH`, `DB_TIMEOUT`, `SQLITE_CACHE_KB` | Database file and tuning. | defaults |
| `IMAGE_SCAN_ROOT`, `INITIAL_IMAGE_SCAN`, `MAX_IMAGE_SIZE`, `MAX_IMAGE_RESPONSE_LIMIT` | Photo folder and limits. **Note:** the backup always reads `data/S.S IMAGE`, so do not point `IMAGE_SCAN_ROOT` elsewhere on Render. | defaults |
| `LOG_DIR`, `LOG_FILE`, `LOG_LEVEL`, `RESOURCE_LOG_INTERVAL_SECONDS`, `MEMORY_LIMIT_MB` | Logging, and how often the "Resource usage" line is written (300 s). | defaults |
| `PORT`, `WAITRESS_THREADS` | Web server port (Render sets `PORT`) and worker threads (8). | Render sets |

The Python version is **not pinned** in the repo: Render uses its default (3.14.3 on 2026-10-18). Library versions **are** pinned in `requirements.txt`.

---

## 3. Daily health check (2 minutes)

1. Open the site; log in; open a car. If it works, the basics are fine.
2. Render dashboard -> your service -> **Logs**. Search for `Resource usage`. Each line looks like:

   `Resource usage: RSS=128.6 MB (limit 512 MB), open file descriptors=9 [sockets=1, database=0, json=0, log=1, other=6]`

   | Number | Meaning | Normal | Worry when |
   |---|---|---|---|
   | **RSS** | Memory the site uses. The server limit is 512 MB. | A fresh start is about **115-130 MB**. A busy, warmed-up site is **240-330 MB**. | Above **450 MB**, or climbing night after night. |
   | **open file descriptors** | Open "handles" (connections, files). | **9** when quiet; about **25-45** when many people are connected. | Above **100**, or going up after each backup. |
   | **sockets** | The part of the above that is network connections. Rises and falls with visitors. | 1 when quiet. | Staying high with nobody online. |

   These two numbers change with traffic and how long the site has been up. A fresh restart looks "better" than a busy site, and that is normal. Compare a reading with **the same site's earlier readings**, not with the fresh values.
3. After the nightly backup (shortly after 02:00 IST) find the line:

   `Cloud backup sync finished (success): open fds 9 -> 9, sockets 1 -> 1, RSS 128.6 -> 131.0 MB (before -> after this run)`

   The "after" numbers should be **back near the "before"** numbers. If sockets are higher after than before, the site also writes a plain warning: *"N more sockets are open after this run than before it -- possible connection leak"*. That means the connection leak (fixed 2026-10-18) is back: tell whoever maintains the site.
4. Other backup lines: `Cloud backup: next scheduled run at <tomorrow> 02:00 IST` (appears after every start; proves the schedule is armed) and `Cloud backup: N new, N changed, N deleted, N unchanged`. In the System panel the Cloud Backup box should say **OK**. **Partial** with a red notice "N deletions held back" is the safety guard (section 8).
5. In the System panel, **Feeder heartbeat** should be fresh (under 15 minutes during shop hours).

**A push was refused.** The site checks every stock push before it writes anything. A push with no cars, no hierarchy rows or no stock rows, a row with a wrong value (for example a quantity that is not a whole number), a repeated car name, or a push that would cut the cars or the stock rows by more than `INTAKE_MAX_DROP_PERCENT` (default 50%) is answered with HTTP 422 and changes nothing: the site keeps serving the last good stock. Where to look: the shop PC's feeder log shows `Cloud sync push failed: HTTP 422 -- <reason>`, and in the System panel the **Feeder heartbeat** stops moving and goes stale. Usually the cause is on the Tally side (a different company open, or an empty export), so fix that and the next push goes through. If the big drop is real (for example a whole range of cars was deleted on purpose): raise `INTAKE_MAX_DROP_PERCENT` on Render (for example `90`), wait for one push to be accepted, then set it back to `50`. Do not leave it raised.

---

## 4. Deploy rules (read before pushing anything to `main`)

* **Never push between 01:55 and 02:10 IST.** The nightly backup runs then and a restart would interrupt it.
* **Avoid Monday-Saturday 10:00-19:30 IST** (shop open). **Every deploy takes the site down for about 90 seconds** (dealers see an error page), because the site has an attached disk and cannot do a zero-downtime swap. Sundays and nights are best.
* **One change at a time.** Leave **20 minutes** between pushes. Watch the logs for **15 minutes** after each (section 3). 
* Before pushing: run `python tests/check_all.py` (section 10). Only fast-forward `main` (never force-push; the history was rewritten once already and that caused trouble on the shop PC).
* **Roll back immediately** if: the health page fails 3 times, new `ERROR`/`Traceback` lines appear that were not there before, the site restarts in a loop or is killed for memory, RSS is above 450 MB, or descriptors are above 100.

---

## 5. After a deploy: how to tell it is healthy

1. Wait until Render shows **Live** (about a minute). A few 502 errors during the swap are normal.
2. `https://<site>/static/shared.css` returns 200 (this is Render's own health check) and the login page loads.
3. Logs show `Starting waitress` **once** (more than once = restart loop), `Cloud backup: next scheduled run at ... 02:00 IST`, and no `ERROR`/`Traceback`.
4. Watch the next three `Resource usage` lines (15 minutes).

---

## 6. Updating the shop PC safely (with the role guard)

**Easiest (2026-10-21 onwards): `tools/shop/SHOP_UPDATE.bat`.** It is one self-contained text file that is pasted into Notepad on the shop PC and saved in the feeder folder (steps for a non-technical person: `docs/SHOP_VISIT.md`). It does everything below by itself (checks, backup of `.env` and `data`, finds the task, updates, proves the guard, waits for a successful push, rolls back on any failure) and shows a big PASS or FAIL. The manual steps below remain as the fallback.

**Never use `git pull` on the shop PC.** History on GitHub was rewritten once; `git pull` can create a merge mess, and an old copy of the code can bring the web app back. Use `update_feeder.bat` (it refuses unless `.env` has exactly one `MACHINE_ROLE=feeder` line, aborts if any tracked file was edited, moves to the latest `origin/main` without `git pull`, installs only the packages the feeder needs from `requirements-feeder.txt`, checks the feeder still loads, restarts the task, and prints old -> new commit). **If the package install or the load check fails, it puts the previous commit back and restarts the task by itself** (exit code 1, message `[ROLLBACK] ...`), so a failed update never leaves the feeder stopped on half-new code.

**Why `requirements-feeder.txt` and not `requirements.txt` on the shop PC:** `requirements.txt` is pinned to what Render installed on Python 3.14. The shop PC runs **Python 3.11**, and an exact pin that needs a newer Python (numpy 2.5.4 needs 3.12+) makes `pip` fail halfway, after git already moved the code. `requirements-feeder.txt` lists only what `feeder.py` imports, as loose ranges, so packages the PC already has are left alone. Checked on 2026-10-18 in a clean Python 3.11.9 environment: it installs, and the feeder mock-Tally + mock-intake test and the guard-refusal checks pass there. **Never run `pip install -r requirements.txt` on the shop PC.**

**First-time switch of a PC that still has the OLD `update_feeder.bat`** (run in a normal Command Prompt in the feeder folder; placeholders in `<>`):

1. Find the real folder and task name first; do not assume them. Folder: `dir C:\tally_stock\feeder.py` (also try `dir C:\tally_feeder\feeder.py`); the folder that has `feeder.py` and `.env` is the one. Task: `schtasks /Query /FO LIST | findstr /I "TaskName"` then `schtasks /Query /TN "<task>" /V /FO LIST | findstr /I "Task To Run, Start In, Status"` ("Start In" confirms the folder). Then `cd /d <that folder>`.
2. Back up `.env` and `data` outside the folder before anything else: `mkdir C:\feeder_backup`, `copy .env C:\feeder_backup\.env`, `xcopy data C:\feeder_backup\data /E /I /Y`. **Never print `.env`** (it holds the sync token): no `type .env`, no `more .env`, no screenshots of it. To look at one line use `findstr /B /I MACHINE_ROLE .env`.
3. Note where you are: `git log -1 --oneline` and `git status --short --untracked-files=no`. If it lists files, **look at them** (`git diff --stat`) before touching anything; `git stash push` sets changes aside safely.
4. Stop the feeder: `schtasks /End /TN "<task>"`.
5. Set the role (never print `.env`): `findstr /B /I MACHINE_ROLE .env`, then `echo.>> .env` and `echo MACHINE_ROLE=feeder>> .env`, then check again: exactly **one** line `MACHINE_ROLE=feeder`. If the task is not called `TallyFeeder`, add `FEEDER_TASK_NAME=<task>` the same way.
6. `git fetch origin`. **If it asks for a login (username/password or a browser sign-in), stop and close the window; do not type credentials.** Nothing has changed yet.
7. `git checkout -B main origin/main`, then `git log -1 --oneline` (it must show the new commit, not the one from step 3) and `dir machine_role.py`. **If `machine_role.py` is missing, or `git log -1` still shows the old commit, stop. Do not run `serve.py`** (an old `serve.py` would start the web app).
8. `.venv\Scripts\python.exe -m pip install -r requirements-feeder.txt`. **If this prints ERROR, put the old code back at once** (the feeder is still stopped from step 4): `git checkout -B main <hash from step 3>`, then `schtasks /Run /TN "<task>"`, and report the error text. Nothing else was changed.
9. Only now (steps 6-8 done, `machine_role.py` present, `git log -1` shows the new commit) prove the guard: `.venv\Scripts\python.exe serve.py` must print the "feeder-only" line and `echo %errorlevel%` must show `3`. Then `.venv\Scripts\python.exe -c "import feeder"` must print nothing.
10. `schtasks /Run /TN "<task>"`, wait 1-2 minutes, then `findstr /C:"machine role" /C:"push succeeded" logs\feeder.log`.

**Undo:** delete or change the `MACHINE_ROLE` line (Notepad) to allow the web app again; to go back to old code: `schtasks /End`, `git checkout -B main <hash from step 3>`, `schtasks /Run`; restore `.env`/`data` from `C:\feeder_backup`. Later updates: double-click `update_feeder.bat`.

---

## 7. Rolling back

* **Fastest (no git):** Render dashboard -> the service -> **Events/Deploys** -> choose the previous good deploy -> **Rollback**. Note: the next push to `main` deploys again from git, so also fix `main` (below).
* **Git way:** `git revert <bad commit>` (this adds a new "undo" commit), then `git push origin main`. Never `git push --force`, never delete branches. Then watch the logs (section 5).
* There is a tag `pre-finish-2026-10-18` marking the code as it was before the October 2026 clean-up.

---

## 8. How the backup works

* Every night at **02:00 IST** (and when an admin presses *Sync now*), the site compares its files with its record of what is on Google Drive and sends only what is new or changed.
* **Database safety:** the database is first copied to a consistent snapshot (not a raw copy of the live file, which could miss recent changes), checked for corruption, and the snapshot is uploaded. If the snapshot fails, the previous good copy on Drive is kept.
* **Nothing is permanently deleted:** a file removed from the site is moved to the **Drive Trash**, where Google keeps it **30 days**. You can restore it from the Trash in Google Drive.
* **Mass-deletion guard:** if one run would trash more than **20 files or 2% of all files (whichever is larger)**, it trashes **nothing** that night, still uploads new files, and finishes as **Partial** with the notice "N deletions held back: local files missing". This protects against an empty or unmounted photo folder wiping the backup. Look at the photo folder. If the files really were removed on purpose, set `BACKUP_MAX_DELETIONS` (for example `1000`) on Render, run *Sync now*, then **remove the setting again**.
* The connection fix (2026-10-18): each run now closes every Drive connection when it finishes. The "sync finished" log line shows open descriptors/sockets/RSS before -> after so a regression is visible the same night.
* Not backed up: popup images (`data/notice_images`, re-uploadable), the share-image cache, logs, and Excel exports.

---

## 9. Restoring from the Drive backup

Drive folder layout mirrors the site: `mappings.db` and the three small `*.json` files at the top, photos under `S.S IMAGE/<car folder>/`.

* **A deleted/changed photo:** open Google Drive -> Trash (or the file's *Version history*) and restore it; the next backup run adopts it.
* **The database:** download `mappings.db` from Drive. Before using it, check it: `sqlite3 mappings.db "PRAGMA integrity_check;"` must say `ok`. Compare row counts (`users`, `images`, `mappings`, `categories`) with what you expect. To put it on the site, suspend the service in Render, copy the file into `data/` over SSH, resume, and check the site. **Do this with help, and keep a copy of the current file first.**
* A restored copy can be a few hours behind the live site (it is as of the last backup).

---

## 10. Adding photos in bulk

Use the push tool (`tools/push_images/`, double-click `push_images.bat`). Read `tools/push_images/SETUP_GUIDE.md` for the one-time setup (SSH key). It: compares first and uploads **nothing** on its first run; never deletes anything on the site; never replaces an existing photo (unless its size differs and you add a flag); refuses more than 200 uploads at once unless you add a flag; keeps its settings in `%LOCALAPPDATA%\SuperSeatings\push_images\` (outside the git folder). Dealers see new photos after the site's next scan.

---

## 11. Checking that nothing broke (tests)

`python tests/check_all.py` runs everything in a **scratch copy** (never your real `data/`, database or `.env`), uses fake Drive/Tally, and ends with `CHECK_ALL: PASS` or `FAIL`. It covers: every route as visitor / dealer / locked admin / unlocked admin (compared with saved "golden" answers), the list of URLs the pages call, stock intake and the "stock updated" version number, activity tracking, the backup (snapshot, Trash, deletion guard, scheduling, connection leak), the role guard, a Render-like start-up, the feeder against a fake Tally, the push tool and `update_feeder.bat`.
If a golden changes *on purpose*, run `python tests/check_all.py --update`, **read `git diff tests/golden`**, and explain each difference in the commit message.
**Not covered:** a real browser/phone, real Google Drive, real Tally, real Render SSH, the installed app (PWA), pixel appearance, production-sized data timing.

---

## 12. What NOT to do

* **Do not push to `main` during 01:55-02:10 IST**, or during shop hours without a reason (every push = ~90 s outage).
* **Do not run the web app, or `git pull`, on the shop PC.** It only runs the feeder.
* **Do not commit `.env`, `data/`, `mappings.db`, logs, tokens or keys.** Do not paste tokens into chat or documents.
* **Do not force-push, rewrite history, or delete branches.**
* **Do not delete photos in bulk from the site's photo folder** without looking at the backup guard first (the backup mirrors removals into the Drive Trash).
* **Do not set `BACKUP_MAX_DELETIONS`** and forget it.
* **Do not judge a restart by memory alone.** A fresh start uses about 120 MB; a busy site uses 240-330 MB. Compare with the same site's earlier readings.
* **Do not change library versions casually** (`requirements.txt` is pinned on purpose for Render; the shop PC uses the separate, loose `requirements-feeder.txt`). Change them deliberately, run the tests, deploy alone, and watch the logs.

---

## 13. Stock Management (admin only)

**Where:** More menu -> **Stock Management**. Admins only (the page and every route behind it refuse dealers and visitors who are not logged in). Written for use on a phone: big text, big buttons. The details of the lists and of the sales data are in section 14.

**How it learns.** Two sources, nothing is typed in by hand. (1) Every time the feeder pushes stock, the site compares it with the previous push and writes down what changed (a restock, an item that sold out, an item that came back). Stock only lists items that are in stock, so an item missing from a push means 0. (2) The shop-side feeder also pushes the sales lines it reads from Tally (section 14). Which cars dealers open comes from the Usage Charts activity log (`user_activity`), not from anything extra.

**The four tabs**

| Tab | What it shows |
|---|---|
| Needs restocking | Three lists: *Sold out*, *Last piece* and *Cars running thin* (section 14). Each design has **Add to order list** with a suggested quantity. |
| Order list | What you plan to order, grouped by category. **Copy as text** (for WhatsApp), **Print**, **Download Excel**, **Mark all/ticked as ordered** (with an optional expected date). |
| Ordered | Open orders with age, expected date and an **Overdue** badge; mark received, cancel, change the date, undo the last action, **Download Excel**. An order is marked **received by itself** when the stock of that item goes up. |
| Slow stock | In stock but nothing sold for 60 or 90 days (from the sales when they reach back that far, otherwise from the stock changes). |

**Safety.** A feeder push where 15% or more of the items (and at least 50) disappear at once is treated as a glitch: nothing is recorded, and only if the same picture arrives three pushes in a row (about 9 minutes) is it accepted as the new baseline, without writing sales. A failure while recording history is logged (`Stock capture failed`) and never changes the answer to the feeder or undoes a push.

**Backups.** All of this lives in tables inside the normal database (`mappings.db`), so the nightly backup and the System panel download include it automatically. Old stock changes are cleaned once a day (after 400 days), sales lines after 800 days. The old `car_interest_daily` and `search_miss_daily` tables are no longer written or read and are left in the database, unused.

**Training Mode** lists cars dealers open most first (control: *Sort by*, with *Original order* as the alternative); that also comes from the Usage Charts activity log.

---

## 14. Stock Management v2 and sales data

The shop keeps only 1 to 3 pieces of a design and reorders 1 to 3 at a time, so "days left" and "running low by quantity" say nothing. The page therefore looks at what **sold**, what is **gone**, and which **cars are thin**.

**Needs restocking: the three lists** (net sold = units sold minus units returned, never below 0)

- **Sold out:** designs not in stock now that sold at least *Sold at least* units in the chosen window (30, 90 or 365 days, default 90), best sellers first, grouped by car (the car with the best seller first). Each row shows how many sold, the last sale, the day it sold out (when stock changes know it) and an *Ordered <date>* badge. With no sales history covering the window, designs whose stock dropped to zero inside it are listed instead; the banner says so.
- **Last piece:** designs with exactly 1 in stock that sold at least *Last piece: sold at least* units in the last 180 days (default 2). Can be hidden with *Show Last piece*.
- **Cars running thin:** a car's *active* designs are those in stock now plus those that sold (net above 0) in the last 365 days. A car is thin when it has at least *Thin car: at least this many designs* (default 4) and at most *Thin car: at most this % of designs in stock* (default 50) in stock. Cars dealers opened most in the last 30 days come first, then the lowest share. *See sold-out designs* lists that car's designs that sold in the last year but are gone.
- **Suggested quantity** is never above *Most pieces you keep of a design* (default 3): the quantity the design last reached when it was restocked (within a year), otherwise a third of its sales in the last 90 days, at least 1. You can change the number before adding it to the order list.

**Settings** (top of the tab, saved on the site): most pieces you keep (1-10, default 3), sold at least (1-50, default 1), count sales over (30, 90 or 365 days), last piece sold at least (1-50, default 2), thin share (10-90 %, default 50), thin minimum designs (2-50, default 4), show Last piece, and whether admin opens count towards a car's interest (default yes). Older settings rows from the first version stay in the database and are ignored.

**Sales intake (what the shop-side feeder sends).** `POST /admin/intake/sales` with the same `X-Intake-Token` as the stock push, JSON: `{"company": <sha256 of the Tally company name, 64 hex>, "from": "YYYY-MM-DD", "to": "YYYY-MM-DD", "lines": [{"v": <voucher key, 8-64 of 0-9a-f>, "d": day, "k": "sale" or "return", "i": item name (1-200), "q": whole number 1-1000000}]}`. The site **replaces** every stored line whose day is between `from` and `to` by the lines sent, in one transaction, so sending the same body twice changes nothing. At most 30,000 lines and 3 MB, a range of at most 400 days; lines with the same voucher, kind and item are added together. No customer, amount or price is ever sent or stored.

| Answer | Meaning |
|---|---|
| 200 | Stored: `{"ok": true, "from", "to", "deleted", "inserted"}`. |
| 401/403 | Missing or wrong token (the same answers as the stock push). |
| 409 `company mismatch` | The company hash differs from the first one the site accepted (the "pin"). Nothing was changed. Not bypassed by the force header. |
| 413 | Body over 3 MB. |
| 422 `refused` | Something in the body is invalid (the message names the field and the line number, never an item name), **or** a guard refused it: an empty push when 50 or more lines are stored in its range, or a push with under 30% of the lines stored in its range when 100 or more are stored. Add the header `X-Intake-Force: 1` to push it anyway (a deliberate clean-up). |
| 503 `busy` | Another sales push, or a database lock, did not clear within 5 seconds. The feeder simply tries again. |

The sales intake has its own lock and a 5-second database timeout; it never waits for, or slows, the stock push. Sales older than 800 days are removed once a day. Items sold under a name that matches no catalogue or stock item are counted in the *Sales data* box on the Stock Management page.

**The Sales data box** (collapsed, at the bottom of the page): last update and the range it covered, days stored, lines, vouchers and designs, a month table (slips, sold, returned, net), the number of sold names that match no catalogue item with a few examples, and the first 6 characters of the pinned company. **Reset company pin** (asks to confirm) forgets the pin but keeps the data; the next sales push pins its company. Use it only if the shop moved to a different Tally company file on purpose: then also consider whether old sales lines should be cleared by a forced push over the whole range.

**Slow stock** uses the sales when their history reaches back at least the chosen 60 or 90 days (a design that never sold in that history counts from the history's first day; a design that came into stock after its last sale starts waiting from then); otherwise it uses the stock changes and keeps the "tracking only since ..." message.

**Day 1.** Before any sales arrive, Sold out falls back to designs whose stock dropped to zero since tracking began, Last piece and Cars running thin stay empty (they need sales to tell what sells), and the banner says "No sales history yet."
