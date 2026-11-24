# Sales sync (shop PC to the website)

The feeder on the shop PC already sends **stock** to the website every few minutes. After the next `SHOP_UPDATE.bat` it also sends **sales**: what sold and what came back (credit notes), item by item. The website uses it for Stock Management (Sold out, Last piece, Cars running thin, Slow stock).

## How it works, in 10 lines

1. The sync asks Tally for **one whole calendar month at a time** (for example "October 2026"). Tally cannot be trusted with other kinds of date questions, so it is never asked any.
2. It asks only for sales slips and credit notes. Tally itself decides which slips count, by their base type, so renamed voucher types (SALES-HO, Sales Gst, MEESHO SALES ...) are found correctly. Delivery notes, purchases and stock journals are left out.
3. **Every 30 minutes** it re-reads the current month. If nothing changed since the last time, **nothing is sent** to the website (it only sends again once a day, as a safety net).
4. **History loads newest first**: last month, then the month before, and so on, one month every few minutes, down to 365 days back. So the website always has one unbroken stretch of months that ends with the current month.
5. **Every night** (after 03:00) it re-reads the current month and the two before it, and sends the ones that changed. This catches slips that were edited or deleted in Tally.
6. It does one small step per feeder cycle (every 3 minutes), after the stock push, and only when that stock push worked. The first cycle after a start does nothing.
7. If Tally is closed or slow, or the website says no, it logs one quiet line and backs off. The stock feeder is never affected.
8. If reading a month takes longer than 40 seconds, it reads less often (the 30 minutes stretch, up to 4 hours) and goes back to normal when Tally is fast again.
9. Progress is kept in `data/sales_sync_state.json` (small, written safely). Delete that file to load the history again from scratch. A missing or damaged file just starts fresh.
10. `SALES_SYNC=off` in `.env` switches it off completely.

## What is sent, and what is never sent

Only this, per line: an anonymous voucher key (it cannot be turned back into the voucher), the day, `sale` or `return`, the item name, a whole number of pieces, and a one-way hash of the company name.

**NEVER sent or logged:** customer or party names, ledger names, amounts, rates, narration, voucher numbers, GUIDs in clear.

Tally hands over the whole slip when it answers (party names and amounts included). The sync reads only the date, the type flags, the cancelled and optional flags, the GUID, the voucher number and the item name and quantity lines. Everything else is dropped on the spot: it is never stored, logged or sent, and a test proves it. The log shows only counts and months, never item names.

## Settings (in the feeder's `.env`)

| Setting | Default | Meaning |
|---|---|---|
| `SALES_SYNC` | `on` | `on`, `off` (nothing happens, Tally is never asked) or `dryrun` (reads Tally and logs what it would send, sends nothing, saves nothing) |
| `SALES_BACKFILL_DAYS` | `365` | how far back history goes (1 to 400) |
| `SALES_INCREMENTAL_MINUTES` | `30` | how often the current month is re-read |
| `SALES_NIGHTLY_HOUR` | `3` | hour (0 to 23, shop PC time) after which the nightly step runs |

## Check it on your laptop (no settings to edit)

Open the company in Tally Prime. A copy of the real company is fine: Tally Prime Educational only limits **entering** vouchers to the 1st, 2nd and last day of a month; reading old vouchers works. Switch on its HTTP server on port 9000.

1. Double-click `tools\dev\SALES_LOCAL_CHECK.bat`, wait about a minute, and send the screenshot or the text.
2. It starts a throw-away copy of the website in a temporary folder (own database, test token), runs the real sales sync against your real Tally, and reads back what the throw-away website stored. It prints one line per step, a table per month with PASS or FAIL, and the totals. The last line says `OVERALL: PASS` or `OVERALL: FAIL`.
3. It never reads your `.env`, never touches your real data folder, never contacts the real website, and deletes its temporary files when it finishes. The last line always says `Nothing was sent to any real website.`
4. It also writes the per-item list of the last full month to a text file and prints where (a path in the Windows temp folder). Open it to compare item by item with Tally.

To compare with Tally by hand: run `tools\shop\SALES_PROBE.bat`. It reads the last 3 calendar months and prints one row per month (sale vouchers, credit notes, sold, returned, net, items, seconds, bytes) and the first 15 lines of the newest month. In Tally open Stock Summary, press F12 and set Show Goods Inwards and Show Goods Outwards to Yes, pick one full past month, and look at the Outwards quantities. Credit notes show as negative outwards, so Tally's Outwards equals the probe's **net**. Tally's Stock Summary also counts delivery notes, which the sync leaves out on purpose, so a difference of that size is expected.

## Go live on the shop PC

The sync reads its `SALES_*` settings **once, when the feeder starts**. After you change a line in the shop's `.env` you must restart the feeder, or nothing changes. To restart it: double-click `SHOP_UPDATE.bat` again (it stops and starts the feeder and checks it pushed), or open Task Scheduler, right-click the feeder task and choose End, then right-click it again and choose Run.

1. Update the shop PC with `SHOP_UPDATE.bat`. This restarts the feeder, and the sync starts by itself. With no `SALES_SYNC` line in `.env` the sync is **on**, so do step 2 before this update if you want to watch it first.
2. Open the shop's `.env` (in the feeder folder, the one with `feeder.py`), add the line `SALES_SYNC=dryrun`, save it, and restart the feeder as described above. Look at `logs/feeder.log` for one day. You should see lines like `Sales sync (dry run): incremental 2026-11 read 146 lines ... would push them`. Nothing is sent in this mode. **A dry run changes nothing:** it saves no progress, so you can leave it on for as long as you like, and switching to `on` afterwards starts from the very beginning.
3. When the log looks right, change that line to `SALES_SYNC=on` (or delete the line), save, and restart the feeder. After a few minutes the Sales data box on the Stock Management page starts to fill, newest month first.
4. If Tally ever feels slow while the history loads, change the line to `SALES_SYNC=off`, save, and restart the feeder. The stock feeder is not affected either way.

## What the log lines mean (logs/feeder.log)

- `Sales sync: incremental 2026-11 pushed 146 lines` (also `backfill` and `nightly`): a month was read and sent.
- `Sales sync: incremental 2026-11 unchanged, no push (146 lines)`: the month is the same as the last time, so nothing was sent. This is the normal quiet state.
- `Sales sync: backfill complete (365 days)`: the history is fully loaded.
- `Sales sync: off`: the setting is off.
- `Sales sync: Tally is not reachable; will try again`: Tally is closed or its HTTP server is off. Nothing to do; it tries again by itself.
- `Sales sync: Tally was too slow to read 2026-10; will try again`: Tally did not answer within 2 minutes. It is not asked again at once.
- `Sales sync: Tally needed 45 s to read a month; the incremental interval is now 60 minutes`: Tally is slow, so the sync reads less often. Logged at most once an hour.
- `Sales sync: could not read the company name from Tally; nothing is sent until it can`: Tally has no company open.
- **404** `Sales sync: waiting for the website to be updated`: the website does not have the sales route yet. The sync waits an hour and tries again. Nothing to do on the shop PC.
- **409** `Sales sync refused: company differs from the one the website already knows`: the website remembers the first Tally company it received. The sync stops for 24 hours. If the shop really moved to another Tally company (for example a new financial-year company), an admin resets the company pin in the Sales data box on the Stock Management page, and the sync then continues.
- **422** `Sales sync refused by the website (422): ...`: the website refused a push and says why (for example it would remove most of the stored lines). It is logged at most once an hour and the sync backs off. Tell the developer the reason shown.
- `Sales sync error: <name>`: an unexpected problem inside the sales step. The stock feeder keeps running. Logged at most once every 10 minutes.

## If something looks wrong

- Nothing in the log: check `SALES_SYNC` is not `off`, and that stock pushes are succeeding (sales only run after a good stock cycle).
- The company name changed (for example a new financial-year company): the sync stops with the 409 line above until an admin resets the company pin in the Sales data box.
- Quantities with decimals (metres, kilos) are rounded down to whole units; the probe shows how many.
