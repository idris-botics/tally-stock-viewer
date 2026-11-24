# Sales sync (shop PC to the website)

The feeder on the shop PC already sends **stock** to the website every few minutes. After the next `SHOP_UPDATE.bat` it also sends **sales**: what sold and what came back (credit notes), item by item. The website uses it for Stock Management (Sold out, Last piece, Cars running thin, Slow stock).

## What is sent, and what is never sent

Only this, per line: an anonymous voucher key (it cannot be turned back into the voucher), the day, `sale` or `return`, the item name, a whole number of pieces, and a one-way hash of the company name.

**NEVER sent or logged:** customer or party names, ledger names, amounts, rates, narration, voucher numbers, GUIDs in clear. The Tally request itself does not ask for them. The log shows only counts and date ranges, never item names.

## How it works

One small step per feeder cycle (every 3 minutes), after the stock push, and only when that stock push worked. The first cycle after a start does nothing.

1. **Incremental** - the last 3 days, every 15 minutes (the very first step ever).
2. **History** - one chunk at a time, at least 2 cycles apart. **History fills newest-first**: the first chunk ends 3 days before today and each next chunk goes further back, so the website always holds one unbroken stretch of days that ends today. It stops at the oldest day (365 days back by default).
3. **Nightly** - the last 36 days once a day after 03:00. This heals vouchers that were changed or deleted in Tally.

Every push **replaces** the website's lines for the days it covers, so sending the same thing twice changes nothing. The chunk size adapts: a read slower than 40 seconds (or a Tally time-out) halves it, a read faster than 10 seconds makes it 1.5 times bigger (1 to 60 days, starting at 31). Large pushes are split in pieces of at most 25,000 lines. Cancelled and optional vouchers are left out; a credit note counts as a return; a custom voucher type counts by its parent type (Sales or Credit Note).

Progress is kept in `data/sales_sync_state.json` (small, written safely). Delete that file to load the history again from scratch. A missing or damaged file just starts fresh.

## Settings (in the feeder's `.env` or environment)

| Setting | Default | Meaning |
|---|---|---|
| `SALES_SYNC` | `on` | `on`, `off` (nothing happens, Tally is never asked) or `dryrun` (reads Tally and logs how many lines it would send, sends nothing, saves nothing) |
| `SALES_BACKFILL_DAYS` | `365` | how far back history goes (1 to 400) |
| `SALES_INCREMENTAL_MINUTES` | `15` | how often the last 3 days are sent |
| `SALES_NIGHTLY_HOUR` | `3` | hour (0 to 23, shop PC time) after which the nightly step runs |

To switch it off in an emergency: put `SALES_SYNC=off` in `.env` and restart the feeder. The stock feeder is not affected either way.

## Log lines (logs/feeder.log)

- `Sales sync: incremental 2026-03-18..2026-03-20 pushed 41 lines` (also `backfill` and `nightly`)
- `Sales sync: backfill complete (365 days)`
- `Sales sync: off`
- `Sales sync: waiting for the website to be updated` - the website answered 404: it does not have the sales route yet. The sync waits an hour and tries again.
- `Sales sync refused: company differs from the one the website already knows` - the website answered 409. The website remembers the first company it received. The sync stops for 24 hours. If the shop really moved to another Tally company, an admin can reset the company pin in the Sales data box on the Stock Management page.
- `Sales sync refused by the website (422): ...` - the website refused a push (the reason is shown). Logged at most once an hour.
- `Sales sync error: <name>` - an unexpected problem inside the sales step. The stock feeder keeps running. Logged at most once every 10 minutes.

## Check it by hand before trusting it: the probe

Double-click `tools\shop\SALES_PROBE.bat` (or run it with `--days 60`). It reads the last 30 days (at most 365) from Tally in small steps and **prints** what the sync would see: counts, a table per month (slips, sold, returned, net) and the first 15 lines. It sends nothing and saves nothing. Compare the month table with Tally's own sales register.

## Testing on a laptop with Tally Prime Educational

Educational mode only allows vouchers dated the 1st, 2nd and the last day of a month, so a test company has few dates. That is enough to prove the chain.

1. Start the website locally (never the real one). Use the local copy with its own `.env`, for example `http://127.0.0.1:5000`.
2. In the laptop's feeder `.env` set `CLOUD_SYNC_URL=http://127.0.0.1:5000/admin/intake/sync_data` and `CLOUD_SYNC_TOKEN=test-token-local` (the same token in the local website's `.env`). NEVER put the real address or the real token there.
3. Open Tally Prime Educational with a test company that has a few sales and credit notes, with its HTTP server on (port 9000 by default).
4. Run `tools\shop\SALES_PROBE.bat --days 90` and check the numbers.
5. Set `SALES_SYNC=dryrun`, start the feeder, and watch `logs/feeder.log` for `would push` lines.
6. Set `SALES_SYNC=on` (or remove the line) and restart. After a few cycles you should see `incremental` then `backfill` lines, and the Sales data box on the Stock Management page fills in.
7. Change a voucher in Tally. The change appears after the next incremental (recent days) or the nightly step (older days).

## If something looks wrong

- Nothing in the log: check `SALES_SYNC` is not `off`, and that stock pushes are succeeding (sales only run after a good stock cycle).
- "waiting for the website": the website has not been updated yet; nothing to do on the shop PC.
- "company differs": see above.
- Quantities with decimals (metres, kilos) are rounded down to whole units; the probe shows how many.

## What the owner should verify on a real Tally

These parts cannot be proven without Tally itself: that Tally accepts the sales request (it falls back to asking for all voucher types and filtering here when it does not take the filter), that the company name is read correctly, and that the parent voucher type of custom voucher types is reported. The probe is the quick way to see all three.
