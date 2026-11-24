"""One-click check of the whole sales chain on a laptop:  Tally (real, on localhost) -> sales sync -> a THROW-AWAY copy of the website.

What it does, in plain words:
  * copies the website code into a temporary folder (never the owner's .env, data/ folder or mappings.db), starts that copy on
    127.0.0.1 with a temporary database and the test token "test-token-local", and waits until it answers;
  * runs the real SalesSync (the same code the feeder uses) against the real Tally, with a fake clock fixed to --today that moves 3
    minutes per cycle without waiting: first an incremental step, then every backfill month down to --days days before --today,
    then one nightly step, then one more incremental step (which must say "unchanged, no push");
  * reads back what the throw-away website stored and prints a table per month, compared with what Tally gave and (with
    --expect clone) with the numbers of the owner's laptop clone of the company;
  * deletes the temporary folder at the end, also after an error.

It never reads .env, never writes into the repo or its data/ folder, never contacts any real website: the pusher below only ever talks
to 127.0.0.1 and aborts before a push if the host is anything else.

Note: the throw-away website has no stock catalogue, so the Stock Management "unmatched names" count on it would be high; that is expected.

Run it with tools\\dev\\SALES_LOCAL_CHECK.bat   (or: .venv\\Scripts\\python.exe tools\\dev\\sales_local_check.py --today 2026-11-24)
"""
import sys

sys.dont_write_bytecode = True      # never leave __pycache__ files in the repo

import argparse
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import sales_sync  # noqa: E402
import tally_sales  # noqa: E402

TOKEN = "test-token-local"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
MAX_CYCLES = 80
CYCLE_SECONDS = 180
COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "data", "logs", "tests", "__pycache__", ".env", "*.db", "*.db-*", "*.zip",
                                     "IMPROVEMENT_PLAN", "brag-output", "ai_tryon_test", ".state_snapshot")
SITE_LAUNCHER = (
    "import os\n"
    "from waitress import serve\n"
    "from app import app, start_background_startup_tasks\n"
    "start_background_startup_tasks()\n"
    "serve(app, host='127.0.0.1', port=int(os.environ['PORT']), threads=4, asyncore_use_poll=True)\n")

# The owner's laptop clone of the company (its Tally data ends on 2026-11-09): month -> (sale vouchers, sold, returned, net)
EXPECTED_CLONE = {
    "2026-04": (493, 1137, 0, 1137), "2026-05": (463, 1012, 3, 1009), "2026-06": (484, 1032, 0, 1032), "2026-07": (436, 897, 0, 897),
    "2026-08": (410, 904, 0, 904), "2026-09": (400, 877, 0, 877), "2026-10": (371, 796, 2, 794), "2026-11": (94, 201, 0, 201)}
EXPECTED_CLONE_TOTALS = {"sale_vouchers": 3151, "credit_notes": 2, "net": 6851}
EXPECTED_CLONE_LIST = {"month": "2026-10", "total": 794, "items": 463}


# ------------------------------------------------------------------------------------------------ small helpers
class FakeClock:
    """The clock the sync sees: fixed to the --today date, 3 minutes further on at every cycle, never waiting."""

    def __init__(self, today):
        self.t = 0.0
        # 00:30 in India on that day: before the nightly hour, so the nightly step comes after the history is loaded
        self.utc = datetime(today.year, today.month, today.day, 0, 30, tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)

    def monotonic(self):
        return self.t

    def utc_now(self):
        return self.utc

    def local_now(self):
        return (self.utc + timedelta(hours=5, minutes=30)).replace(tzinfo=None)

    def advance(self, seconds):
        self.t += seconds
        self.utc += timedelta(seconds=seconds)


class MeasuringReader:
    """Wraps the real TallyReader and remembers every month it read (what, how long, how big)."""

    def __init__(self, reader):
        self.reader = reader
        self.reads = []

    def company_name(self):
        return self.reader.company_name()

    def fetch_month(self, year, month):
        lines, stats = self.reader.fetch_month(year, month)
        self.reads.append({
            "month": sales_sync.month_key(year, month), "stats": dict(stats), "lines": len(lines), "seconds": stats["seconds"],
            "bytes": stats["bytes"], "sold": sum(l["qty"] for l in lines if l["kind"] == "sale"),
            "returned": sum(l["qty"] for l in lines if l["kind"] == "return"), "items": len({l["item"] for l in lines})})
        return lines, stats


class LogCollector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return os.path.commonpath([path, folder]) == folder


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Check Tally -> sales sync -> a throw-away website, on this laptop.")
    parser.add_argument("--today", required=True, help="the date the sync should treat as today, YYYY-MM-DD (a fixed test date)")
    parser.add_argument("--days", type=int, default=365, help="how far back history goes (1 to 400, default 365)")
    parser.add_argument("--tally-url", default="http://localhost:9000", help="Tally's address (this laptop only)")
    parser.add_argument("--expect", choices=("clone", "none"), default="clone",
                        help="compare with the numbers of the owner's laptop clone (default) or only check the chain")
    args = parser.parse_args(argv)
    args.today = date.fromisoformat(args.today)
    args.days = max(1, min(400, args.days))
    return args


# ------------------------------------------------------------------------------------------------ the throw-away website
def start_site(tmp, out):
    """Copies the website code into tmp and starts it on 127.0.0.1. Returns (process, base_url, database_path)."""
    site = os.path.join(tmp, "site")
    shutil.copytree(ROOT, site, ignore=COPY_IGNORE)
    os.makedirs(os.path.join(site, "data"))
    os.makedirs(os.path.join(site, "logs"))
    assert not os.path.exists(os.path.join(site, ".env")), "the scratch copy must not contain a .env"
    port = free_port()
    db_path = os.path.join(site, "data", "check.db")
    for path in (site, db_path):
        assert inside(path, tmp), "every folder the check uses must be inside its temporary folder"
    env = {k: v for k, v in os.environ.items() if k.upper() != "MACHINE_ROLE" and not k.startswith(("GDRIVE", "CLOUD_SYNC", "TEST_"))}
    env.update(PYTHONUNBUFFERED="1", PORT=str(port), DISABLE_TALLY_SCHEDULING="1", INTAKE_SYNC_TOKEN=TOKEN, DB_PATH="data/check.db",
               FLASK_SECRET_KEY="local-check", INITIAL_IMAGE_SCAN="0", LOG_FILE="logs/app.log", SYSTEM_ACCESS_TOKEN="local-check",
               ACCOUNTS_ACCESS_PASSWORD="local-check", RESCAN_TRIGGER_TOKEN="local-check")
    server_out = open(os.path.join(tmp, "server.out"), "ab")
    process = subprocess.Popen([sys.executable, "-c", SITE_LAUNCHER], cwd=site, env=env, stdout=server_out, stderr=server_out)
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(base + "/health", timeout=3):
                out("throw-away website is running on 127.0.0.1 (temporary folder, test token)")
                return process, base, db_path
        except Exception:
            time.sleep(0.3)
    stop_site(process)
    raise RuntimeError("the throw-away website did not start")


def stop_site(process):
    process.terminate()
    try:
        process.wait(10)
    except Exception:
        process.kill()


def make_pusher(base):
    url = base + "/admin/intake/sales"
    counter = {"pushes": 0}

    def push(payload):
        assert urlsplit(url).hostname == "127.0.0.1", "this check only ever pushes to 127.0.0.1"
        counter["pushes"] += 1
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                         headers={"X-Intake-Token": TOKEN, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
    push.counter = counter
    return push


# ------------------------------------------------------------------------------------------------ the check
def month_range(today, floor_key):
    months, current = [], (today.year, today.month)
    floor = sales_sync.parse_month(floor_key)
    while current >= floor:
        months.append(sales_sync.month_key(*current))
        current = sales_sync.previous_month(*current)
    return months


def main(argv=None, out=print):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    started = time.monotonic()
    if urlsplit(args.tally_url).hostname not in LOCAL_HOSTS:
        out("This check only talks to the Tally on this laptop (localhost). Nothing was sent to any real website.")
        return 2
    out(f"Sales local check: Tally at {args.tally_url}, the sync's date is {args.today}, history {args.days} days")
    tmp = tempfile.mkdtemp(prefix="sales_local_check_")
    process = None
    try:
        reader = MeasuringReader(tally_sales.TallyReader(tally_url=args.tally_url))
        try:
            name = reader.company_name()
        except tally_sales.TallyReadError as exc:
            out("Tally is not reachable: " + str(exc) + ".")
            out("Open Tally Prime with the company loaded and its HTTP server on (port 9000), then run this again.")
            out("Nothing was sent to any real website.")
            return 2
        if not name:
            out("Tally answered, but did not say which company is open. Load the company and run this again.")
            out("Nothing was sent to any real website.")
            return 2
        out("Tally company (first 8 characters of its hash): " + tally_sales.company_hash(name)[:8])
        process, base, db_path = start_site(tmp, out)
        state_path = os.path.join(tmp, "sales_sync_state.json")
        assert inside(state_path, tmp)
        clock, pusher = FakeClock(args.today), make_pusher(base)
        settings = sales_sync.SalesSettings(mode="on", backfill_days=args.days, incremental_minutes=30, nightly_hour=3)
        sync = sales_sync.SalesSync(state_path=state_path, reader=reader, pusher=pusher, settings=settings, clock=clock)
        collector = LogCollector()
        logger = logging.getLogger("feeder")
        old_level, old_disable = logger.level, logging.root.manager.disable
        logging.disable(logging.NOTSET)
        logger.setLevel(logging.INFO)
        logger.addHandler(collector)
        try:
            outcome = run_sync(sync, clock, reader, pusher, collector, out)
        finally:
            logger.removeHandler(collector)
            logger.setLevel(old_level)
            logging.disable(old_disable)
        passed = report(args, sync, reader, outcome, db_path, out)
        out("")
        out(f"total time {time.monotonic() - started:.1f} s; slowest single Tally read {max((r['seconds'] for r in reader.reads), default=0.0):.1f} s")
        out("")
        out("OVERALL: " + ("PASS" if passed else "FAIL"))
        out("Nothing was sent to any real website.")
        return 0 if passed else 1
    finally:
        if process is not None:
            stop_site(process)
        for _ in range(10):
            shutil.rmtree(tmp, ignore_errors=True)
            if not os.path.exists(tmp):
                break
            time.sleep(0.5)


def run_sync(sync, clock, reader, pusher, collector, out):
    """Drives the steps in the required order. Returns {'cycles', 'final_unchanged', 'final_pushes'}."""
    state = {"cycles": 0, "step": 0}

    def cycle():
        if state["cycles"] >= MAX_CYCLES:
            return "limit"
        state["cycles"] += 1
        marks = (len(collector.records), len(reader.reads), pusher.counter["pushes"])
        label = sync.step(True)
        clock.advance(CYCLE_SECONDS)
        if label:
            state["step"] += 1
            out(f"step {state['step']}: {label}")
            for read in reader.reads[marks[1]:]:
                stats = read["stats"]
                out(f"   read {read['month']}: {stats['sale_vouchers']} sale vouchers, {stats['credit_notes']} credit notes, "
                    f"{read['lines']} lines, {read['seconds']:.1f} s, {read['bytes']} bytes")
            for message in collector.records[marks[0]:]:
                if message.startswith("Sales sync"):
                    out("   " + message)
            state["last_pushes"] = pusher.counter["pushes"] - marks[2]
            state["last_messages"] = collector.records[marks[0]:]
        return label

    def until(predicate):
        while True:
            label = cycle()
            if label == "limit" or predicate(label):
                return label != "limit"

    ok = until(lambda label: label == "incremental")
    ok = ok and until(lambda label: sync.state["backfill_done"])
    while clock.local_now().hour < sync.settings.nightly_hour:          # let the night pass: the nightly step is due after its hour
        clock.advance(1800)
    ok = ok and until(lambda label: sync.state["last_nightly_day"] == clock.local_now().date().isoformat())
    clock.advance(31 * 60)
    ok = ok and until(lambda label: label == "incremental")
    final_unchanged = ok and state.get("last_pushes", 1) == 0 and any("unchanged, no push" in m for m in state.get("last_messages", []))
    return {"cycles": state["cycles"], "complete": ok, "final_unchanged": final_unchanged}


def report(args, sync, reader, outcome, db_path, out):
    """Reads back what the throw-away website stored, prints the month table and the verdicts. True when everything matches."""
    out("")
    out(f"{outcome['cycles']} cycles used" + ("" if outcome["complete"] else " - the steps did NOT finish in time"))
    connection = sqlite3.connect(db_path)
    try:
        stored = {row[0]: row[1:] for row in connection.execute(
            "SELECT substr(day, 1, 7), COALESCE(SUM(CASE WHEN kind = 'sale' THEN qty END), 0), "
            "COALESCE(SUM(CASE WHEN kind = 'return' THEN qty END), 0), COUNT(DISTINCT item_key) FROM sales_lines GROUP BY 1")}
        total_lines, first_day, last_day, vouchers, distinct_items = connection.execute(
            "SELECT COUNT(*), MIN(day), MAX(day), COUNT(DISTINCT voucher_key), COUNT(DISTINCT item_key) FROM sales_lines").fetchone()
        last_full = sales_sync.previous_month(args.today.year, args.today.month)
        last_full_key = sales_sync.month_key(*last_full)
        item_rows = connection.execute(
            "SELECT MAX(item_name), SUM(CASE WHEN kind = 'sale' THEN qty ELSE -qty END) FROM sales_lines WHERE substr(day, 1, 7) = ? "
            "GROUP BY item_key", (last_full_key,)).fetchall()
    finally:
        connection.close()
    out(f"the website stored {total_lines} lines, {vouchers} vouchers, {distinct_items} distinct items, days {first_day} .. {last_day}")
    latest = {}
    for read in reader.reads:
        latest[read["month"]] = read
    passed = outcome["complete"] and outcome["final_unchanged"]
    out("")
    out("month    sale vouchers (Tally)    sold  returned     net  items   stored = read   expected")
    totals = {"sale_vouchers": 0, "credit_notes": 0, "net": 0}
    for key in month_range(args.today, sync.state["backfill_floor"]):
        read = latest.get(key)
        sold, returned, items = stored.get(key, (0, 0, 0))
        vouchers_read = read["stats"]["sale_vouchers"] if read else 0
        matches = bool(read) and (sold, returned, items) == (read["sold"], read["returned"], read["items"])
        verdict = "-"
        if args.expect == "clone":
            expected = EXPECTED_CLONE.get(key, (0, 0, 0, 0))
            verdict = "PASS" if (vouchers_read, sold, returned, sold - returned) == expected else "FAIL"
            if key == EXPECTED_CLONE_LIST["month"] and items != EXPECTED_CLONE_LIST["items"]:
                verdict = "FAIL"
            passed = passed and verdict == "PASS"
        passed = passed and matches
        totals["sale_vouchers"] += vouchers_read
        totals["credit_notes"] += read["stats"]["credit_notes"] if read else 0
        totals["net"] += sold - returned
        out(f"{key}  {vouchers_read:19d} {sold:7d} {returned:9d} {sold - returned:7d} {items:6d}   {'yes' if matches else 'NO':>13}   {verdict}")
    out("")
    out(f"totals: {totals['sale_vouchers']} sale vouchers, {totals['credit_notes']} credit notes, net {totals['net']}")
    if args.expect == "clone" and sales_sync.parse_month(sync.state["backfill_floor"]) <= (2026, 4):
        ok = totals == EXPECTED_CLONE_TOTALS
        out(f"expected totals: {EXPECTED_CLONE_TOTALS['sale_vouchers']} sale vouchers, {EXPECTED_CLONE_TOTALS['credit_notes']} credit notes, "
            f"net {EXPECTED_CLONE_TOTALS['net']}: {'PASS' if ok else 'FAIL'}")
        passed = passed and ok
    out("second run (the last incremental step): " + ("unchanged, no push: yes" if outcome["final_unchanged"] else "unchanged, no push: NO"))
    names = sorted(item_rows, key=lambda row: (row[0] or ""))
    list_total, list_items = sum(row[1] for row in item_rows), len(item_rows)
    path = os.path.join(tempfile.gettempdir(), f"sales_local_check_{last_full_key}_items.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(f"{name} | {net}\n" for name, net in names))
    out("")
    out(f"{last_full_key} per-item list (item | net units) written to: {path}")
    out(f"{last_full_key} total net units: {list_total}   items: {list_items}")
    if args.expect == "clone" and last_full_key == EXPECTED_CLONE_LIST["month"]:
        ok = (list_total, list_items) == (EXPECTED_CLONE_LIST["total"], EXPECTED_CLONE_LIST["items"])
        out(f"expected {EXPECTED_CLONE_LIST['total']} net units and {EXPECTED_CLONE_LIST['items']} items: {'PASS' if ok else 'FAIL'}")
        passed = passed and ok
    return passed


if __name__ == "__main__":
    sys.exit(main())
