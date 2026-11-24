"""Read-only look at what the sales sync would send. Reads sales vouchers and credit notes from Tally for the last 3 calendar months
(this month and the 2 before it), ONE CALENDAR MONTH at a time, and PRINTS a summary. It never pushes anything and never writes any file.

Prints: the first 8 characters of the company hash, a table per month (sale vouchers, credit notes, units sold, units returned, net,
distinct items, seconds, bytes) and the first 15 lines (day, sale/return, item, quantity) of the newest month that has any. Never a
customer, ledger, amount, rate, narration, voucher number or key.

Run it with tools\\shop\\SALES_PROBE.bat   (or: .venv\\Scripts\\python.exe tools\\shop\\sales_probe.py [--today 2026-11-24])
"""
import os
import sys
import time
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import sales_sync  # noqa: E402
import tally_sales  # noqa: E402

MONTHS_SHOWN, SAMPLE_LINES, PAUSE_SECONDS = 3, 15, 0.5


def parse_today(argv):
    """--today YYYY-MM-DD or --today=YYYY-MM-DD, else None (the probe then asks the same clock the sync uses)."""
    value = None
    for index, arg in enumerate(argv):
        if arg == "--today" and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith("--today="):
            value = arg.split("=", 1)[1]
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def main(argv=None, reader=None, out=print, today=None, pause=PAUSE_SECONDS):
    argv = sys.argv[1:] if argv is None else argv
    today = parse_today(argv) or today or sales_sync.RealClock().local_now().date()
    reader = reader or tally_sales.TallyReader()
    months = [(today.year, today.month)]
    while len(months) < MONTHS_SHOWN:
        months.append(sales_sync.previous_month(*months[-1]))
    out("Sales probe: reading the last 3 calendar months from Tally, one month at a time. Nothing is sent anywhere and nothing is saved.")
    out("request mode: calendar months")
    results = []
    try:
        name = reader.company_name()
        for index, (year, month) in enumerate(months):
            lines, stats = reader.fetch_month(year, month)
            results.append((sales_sync.month_key(year, month), lines, stats))
            if index + 1 < len(months):
                time.sleep(pause)
    except tally_sales.TallyReadError as exc:
        out("Could not read from Tally: " + str(exc) + ".")
        out("Check that Tally is open, the company is loaded, and Tally's HTTP server is on (the feeder uses the same address, port 9000 by default).")
        return 1
    out("")
    out("Company (first 8 characters of its hash): " + (tally_sales.company_hash(name)[:8] if name else "could not be read"))
    out("")
    out("month    sale vouchers  credit notes     sold  returned     net  items  seconds       bytes")
    for key, lines, stats in sorted(results):
        sold = sum(line["qty"] for line in lines if line["kind"] == "sale")
        returned = sum(line["qty"] for line in lines if line["kind"] == "return")
        items = len({line["item"] for line in lines})
        out(f"{key}  {stats['sale_vouchers']:12d} {stats['credit_notes']:13d} {sold:8d} {returned:9d} {sold - returned:7d} {items:6d} "
            f"{stats['seconds']:8.1f} {stats['bytes']:11d}")
    left_out = {name_: sum(stats[name_] for _key, _lines, stats in results) for name_ in
                ("skipped_other_type", "skipped_cancelled", "skipped_optional", "skipped_no_date", "dropped_out_of_range", "fractional")}
    out("")
    out(f"left out: {left_out['skipped_cancelled']} cancelled, {left_out['skipped_optional']} optional, {left_out['skipped_other_type']} "
        f"other voucher types, {left_out['skipped_no_date']} without a date, {left_out['dropped_out_of_range']} outside the month, "
        f"{left_out['fractional']} fractional quantities rounded down")
    newest = next(((key, lines) for key, lines, _stats in sorted(results, reverse=True) if lines), None)
    out("")
    if newest is None:
        out("no sales lines in these 3 months")
        return 0
    key, lines = newest
    out(f"first {min(SAMPLE_LINES, len(lines))} lines of {key} (day, kind, item, quantity):")
    for line in sorted(lines, key=lambda item: (item["day"], item["voucher_key"]))[:SAMPLE_LINES]:
        out(f"  {line['day']}  {line['kind']:6s} {line['item']}  x{line['qty']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
