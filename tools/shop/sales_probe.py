"""Read-only look at what the sales sync would send. Reads sales vouchers and credit notes from Tally for the last N days
(default 30, at most 365), in small steps, and PRINTS a summary. It never pushes anything and never writes any file.

Prints: the first 8 characters of the company hash, counts, a per-month table (sales slips, units sold, units returned, net) and the
first 15 lines (day, sale/return, item, quantity). Never a customer, ledger, amount, rate, narration, voucher number or key.

Run it with tools\\shop\\SALES_PROBE.bat   (or: .venv\\Scripts\\python.exe tools\\shop\\sales_probe.py --days 30)
"""
import os
import sys
import time
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tally_sales  # noqa: E402

DEFAULT_DAYS, MAX_DAYS, CHUNK_DAYS, SAMPLE_LINES, PAUSE_SECONDS = 30, 365, 31, 15, 0.5


def parse_days(argv):
    """--days N or --days=N: default 30, at least 1, at most 365; anything unreadable gives the default."""
    value = None
    for index, arg in enumerate(argv):
        if arg == "--days" and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith("--days="):
            value = arg.split("=", 1)[1]
    try:
        return max(1, min(MAX_DAYS, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def main(argv=None, reader=None, out=print, today=None, pause=PAUSE_SECONDS):
    argv = sys.argv[1:] if argv is None else argv
    days = parse_days(argv)
    reader = reader or tally_sales.TallyReader()
    today = today or date.today()
    first_day = today - timedelta(days=days)
    out(f"Sales probe: reading the last {days} days from Tally. Nothing is sent anywhere and nothing is saved.")
    try:
        name = reader.company_name()
        lines, totals = [], tally_sales.new_stats()
        end = today
        while end >= first_day:
            start = max(end - timedelta(days=CHUNK_DAYS - 1), first_day)
            lines += reader.fetch_sales_lines(start, end)
            for key, value in reader.last_stats.items():
                totals[key] += value
            end = start - timedelta(days=1)
            if end >= first_day:
                time.sleep(pause)
    except tally_sales.TallyReadError as exc:
        out("Could not read from Tally: " + str(exc) + ".")
        out("Check that Tally is open, the company is loaded, and Tally's HTTP server is on (the feeder uses the same address, port 9000 by default).")
        return 1
    out("")
    out("Company (first 8 characters of its hash): " + (tally_sales.company_hash(name)[:8] if name else "could not be read"))
    out(f"vouchers: {totals['vouchers']}")
    out(f"lines: {len(lines)}")
    out(f"left out: {totals['cancelled']} cancelled, {totals['optional']} optional, {totals['other_types']} other voucher types, "
        f"{totals['fractional']} fractional quantities rounded down, {totals['bad']} unreadable")
    months = {}
    for key in sorted({(line["voucher_key"], line["day"].strftime("%Y-%m")) for line in lines if line["kind"] == "sale"}):
        months.setdefault(key[1], [0, 0, 0])[0] += 1
    for line in lines:
        row = months.setdefault(line["day"].strftime("%Y-%m"), [0, 0, 0])
        row[1 if line["kind"] == "sale" else 2] += line["qty"]
    out("")
    out("month     slips   sold  returned    net")
    for month in sorted(months):
        slips, sold, returned = months[month]
        out(f"{month}  {slips:6d} {sold:6d} {returned:9d} {sold - returned:6d}")
    out("")
    out(f"first {min(SAMPLE_LINES, len(lines))} lines (day, kind, item, quantity):")
    for line in sorted(lines, key=lambda item: (item["day"], item["voucher_key"]))[:SAMPLE_LINES]:
        out(f"  {line['day']}  {line['kind']:6s} {line['item']}  x{line['qty']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
